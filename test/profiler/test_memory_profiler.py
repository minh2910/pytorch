# Owner(s): ["oncall: profiler"]
import functools
import gc
import itertools as it
import textwrap
from typing import Callable, Dict, Iterator, List, Optional, Tuple

import torch
from torch._C._profiler import _EventType
from torch.profiler import _memory_profiler, _utils
from torch.testing._internal.common_utils import run_tests, skipIfTorchDynamo, TestCase


profile = functools.partial(
    torch.profiler.profile, record_shapes=True, profile_memory=True, with_stack=True
)


@skipIfTorchDynamo("TorchDynamo removes profiler altogether.")
class TestMemoryProfiler(TestCase):
    def test_config_check(self) -> None:
        with torch.profiler.profile() as prof:
            pass

        pattern = r"record_shapes=True, profile_memory=True, with_stack=True"
        with self.assertRaisesRegex(ValueError, pattern):
            prof._memory_profile()

        with torch.profiler.profile(record_shapes=True, with_stack=True) as prof:
            pass

        pattern = r"^profile_memory=True required for memory profiling\.$"
        with self.assertRaisesRegex(ValueError, pattern):
            prof._memory_profile()

        with profile() as prof:
            pass

        self.assertIsInstance(prof._memory_profile(), _memory_profiler.MemoryProfile)


class ScaleLayer(torch.nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.scale = torch.nn.Parameter(torch.rand(()), requires_grad=True)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x * self.scale


@skipIfTorchDynamo("TorchDynamo changes Python calls that memory profiling relies on.")
class TestIdentifyGradients(TestCase):
    def gradient_detected(
        self,
        prof: torch.profiler.profile,
        ctx: _EventType,
        grad_tensor: torch.Tensor,
        parameter: Optional[torch.Tensor] = None,
    ) -> None:

        # This is not an exhaustive check, but for the purpose of unit testing
        # it is sufficient.
        def key_matches_tensor(key, tensor) -> bool:
            # Vacuous case.
            if tensor is None:
                return True

            if key is None:
                return False

            return tensor.storage().data_ptr() == key.storage_ptr

        tree = prof.profiler.kineto_results.experimental_event_tree()
        for node in _utils.traverse_dfs(tree):
            for p_key, p_grad_key in _memory_profiler.extract_gradients(node):
                if node.tag == ctx and key_matches_tensor(p_grad_key, grad_tensor):
                    if parameter is None:
                        return True  # Don't need to check parameter; we're done.

                    elif p_key is not None:
                        # For a complex workflow a gradient could correspond to
                        # different parameters at different points in a trace.
                        # However this will not happen in the relatively simple
                        # cases tested here, so if `extract_gradients` identifies
                        # the parameter corresponding to a particular gradient it
                        # must be the one we expect.
                        self.assertTrue(key_matches_tensor(p_key, parameter))
                        return True

        return False

    def assertGradientDetected(self, name: str, *args, **kwargs) -> None:
        self.assertTrue(
            self.gradient_detected(*args, **kwargs),
            f"Failed to identify gradient `{name}` from profile.",
        )

    def assertOnlyGradients(
        self, prof: torch.profiler.profile, tensors: Iterator[torch.Tensor]
    ) -> None:
        allowed_set = {t.storage().data_ptr() for t in tensors}

        tree = prof.profiler.kineto_results.experimental_event_tree()
        for node in _utils.traverse_dfs(tree):
            for _, p_grad_key in _memory_profiler.extract_gradients(node):
                self.assertTrue(
                    p_grad_key.storage_ptr in allowed_set,
                    f"Tensor wrongly marked as gradient: {node.name}: {p_grad_key}",
                )

    def test_extract_gradients_low_level(self) -> None:
        x = torch.ones((1,))
        w0 = torch.ones((1,), requires_grad=True)
        w1 = torch.ones((1,), requires_grad=True)

        def check(cold_start: bool):
            self.assertEqual(w0.grad is None, cold_start)
            self.assertEqual(w1.grad is None, cold_start)
            with profile() as prof:
                z = x.expand(4) * w0
                (z * w1).sum().backward()

            # Gradient detection through op inspection does not provide a
            # reference to the parameter corresponding to the gradient.
            self.assertGradientDetected("w0", prof, _EventType.TorchOp, w0.grad)
            self.assertGradientDetected("w1", prof, _EventType.TorchOp, w1.grad)
            self.assertOnlyGradients(prof, (w0.grad, w1.grad))

        check(cold_start=True)
        check(cold_start=False)

    def test_extract_gradients_from_module(self) -> None:
        model = torch.nn.Sequential(torch.nn.Linear(2, 1), ScaleLayer())
        named_parameters = {name: p for name, p in model.named_parameters()}
        self.assertEqual(len(named_parameters), 3)

        def assert_only_gradients(prof: torch.profiler.profile):
            gradients = tuple(i.grad for i in named_parameters.values())
            self.assertFalse(any(i is None for i in gradients))
            self.assertOnlyGradients(prof, gradients)

        def check(cold_start: bool):
            x = torch.ones((2, 2))
            with profile() as prof:
                model(x).sum().backward()

            for name, p in named_parameters.items():
                # The first time we run a module none of the `.grad` fields
                # have been initialized. This is fine; in that case we can
                # detect everything we need in the profiled section.
                self.assertNotEqual(
                    self.gradient_detected(prof, _EventType.PyCall, p.grad, p),
                    cold_start,
                    name,
                )

                # Op based detection should still identify the gradients.
                self.assertGradientDetected(name, prof, _EventType.TorchOp, p.grad)
            assert_only_gradients(prof)

            # We can detect gradients even when `.backward()` is not called.
            with profile() as prof:
                model(torch.ones((2, 2)))

            for name, p in named_parameters.items():
                self.assertGradientDetected(name, prof, _EventType.PyCall, p.grad, p)
                self.assertFalse(
                    self.gradient_detected(prof, _EventType.TorchOp, p.grad), name
                )
            assert_only_gradients(prof)

        check(cold_start=True)
        check(cold_start=False)

    def _test_extract_gradients_from_optimizer(self, set_to_none: bool) -> None:

        x = torch.ones((1,))
        w0 = torch.ones((1,), requires_grad=True)
        w1 = torch.ones((1,), requires_grad=True)
        optimizer = torch.optim.SGD((w0, w1), lr=0.1, momentum=0.9)

        def check(cold_start: bool):
            self.assertEqual(w0.grad is None, cold_start)
            self.assertEqual(w1.grad is None, cold_start)
            with profile() as prof:
                optimizer.zero_grad(set_to_none=set_to_none)
                z = x.expand(4) * w0
                (z * w1).sum().backward()
                optimizer.step()

            # Optimizer instrumentation runs late in the step, so we can detect
            # gradients for both cold and warm start.
            self.assertGradientDetected("w0", prof, _EventType.PyCall, w0.grad, w0)
            self.assertGradientDetected("w1", prof, _EventType.PyCall, w1.grad, w1)

            self.assertGradientDetected("w0", prof, _EventType.TorchOp, w0.grad)
            self.assertGradientDetected("w1", prof, _EventType.TorchOp, w1.grad)
            self.assertOnlyGradients(prof, (w0.grad, w1.grad))

            with profile() as prof:
                for _ in range(2):
                    optimizer.zero_grad(set_to_none=set_to_none)
                    z = x.expand(4) * w0
                    (z * w1).sum().backward()
                    optimizer.step()

            # Inspected state is cached, so if we replace gradients (as is the
            # case for `set_to_none=True`) our python instrumentation will not
            # see them.
            # TODO(robieta): Should `.step()` be excluded from caching?
            self.assertNotEqual(
                self.gradient_detected(prof, _EventType.PyCall, w0.grad, w0),
                set_to_none,
            )

            self.assertNotEqual(
                self.gradient_detected(prof, _EventType.PyCall, w1.grad, w1),
                set_to_none,
            )

            if set_to_none:
                with self.assertRaisesRegex(AssertionError, "Tensor wrongly marked"):
                    self.assertOnlyGradients(prof, (w0.grad, w1.grad))

        check(cold_start=True)
        check(cold_start=False)

    def test_extract_gradients_from_optimizer(self) -> None:
        self._test_extract_gradients_from_optimizer(set_to_none=False)

    def test_extract_gradients_from_optimizer_set_to_none(self) -> None:
        self._test_extract_gradients_from_optimizer(set_to_none=True)

    def test_extract_gradients_from_module_and_optimizer(self) -> None:
        # Module and optimizer are thoroughly tested individually and should be
        # additive. Thus we can manage with a lightweight check that they don't
        # interact adversely.
        model = torch.nn.Sequential(torch.nn.Linear(2, 1), ScaleLayer())
        optimizer = torch.optim.SGD(model.parameters(), lr=0.1, momentum=0.9)
        with profile() as prof:
            model(torch.ones((2, 2))).sum().backward()
            optimizer.step()

        self.assertGradientDetected(
            "weight", prof, _EventType.PyCall, model[0].weight.grad, model[0].weight
        )


@skipIfTorchDynamo("TorchDynamo removes profiler altogether.")
class TestDataFlow(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.maxDiff = None

    @staticmethod
    def formatSchemas(
        prof: torch.profiler.profile, indent: int = 12
    ) -> Tuple[Tuple[str, Tuple[bool, ...]], ...]:
        tree = prof.profiler.kineto_results.experimental_event_tree()
        out: List[Tuple[str, Tuple[bool, ...]]] = []
        for node in _utils.traverse_dfs(tree):
            if node.tag == _EventType.TorchOp:
                e = node.extra_fields
                schemas = _memory_profiler.SchemaMatcher.match_schemas(e)
                name = node.name
                if len(schemas) == 1:
                    name = f"{name}.{schemas[0].overload_name}"
                elif len(schemas) > 1:
                    name = f"{name}.{{{', '.join(s.overload_name for s in schemas)}}}"

                out.append((name, _memory_profiler.SchemaMatcher.inputs_are_mutable(e)))
        return tuple(out)

    @staticmethod
    def _run_and_format_data_flow(
        inputs: Dict[str, torch.Tensor],
        f: Callable[..., Optional[Dict[str, torch.Tensor]]],
        indent: int = 12,
    ) -> str:
        with profile() as prof:
            outputs = f(**inputs) or {}
            gc.collect()

        memory_profile = prof._memory_profile()
        graph = memory_profile._data_flow_graph
        storage_to_id = {key.storage_ptr: key.id for key in graph._active_version}

        lines: List[str] = []
        for name, t in it.chain(inputs.items(), outputs.items()):
            lines.append(f"{name + ':':<8} T{storage_to_id[t.storage().data_ptr()]}")
            if t.grad is not None:
                grad_id = storage_to_id[t.grad.storage().data_ptr()]
                lines.append(f"{name + '.grad:':<9} T{grad_id}")

        if lines:
            lines.append("")

        for node in graph.flow_nodes:
            destroyed = {k for k, v in node._edges.items() if v.is_deletion}

            inputs: List[str] = []
            for key, (_, v) in node.inputs.items():
                inputs.append(f"T{key.id}(v{v}{'*' if key in destroyed else ''})")

            outputs = [f"T{key.id}(v{v})" for key, v in node.outputs.items()]
            if inputs or outputs:
                event_name = node._event.name.replace("torch::autograd::", "")
                lines.append(
                    f"{event_name:<25} {', '.join(inputs):<15}  ->  {', '.join(outputs)}"
                )

        return textwrap.indent("\n".join([l.rstrip() for l in lines]), " " * indent)

    def test_match_schemas(self) -> None:
        with profile() as prof:
            x = torch.ones((1,)).mul(2).add_(2)
            _ = torch.sin(x, out=torch.empty_like(x))

        self.assertEqual(
            self.formatSchemas(prof),
            (
                ("aten::ones.", (False,) * 5),
                ("aten::empty.memory_format", (False,) * 6),
                #
                # fill_.Scalar(Tensor(a!) self, Scalar value) -> Tensor(a!)
                ("aten::fill_.Scalar", (True, False)),
                ("aten::mul.Tensor", (False, False)),
                ("aten::to.dtype", (False,) * 5),
                ("aten::_to_copy.", (False,) * 7),
                ("aten::empty_strided.", (False,) * 6),
                #
                # copy_(Tensor(a!) self, Tensor src, bool non_blocking=False) -> Tensor(a!)
                ("aten::copy_.", (True, False, False)),
                #
                # add_.Tensor(Tensor(a!) self, Tensor other, *, Scalar alpha=1) -> Tensor(a!)
                ("aten::add_.Tensor", (True, False, False)),
                ("aten::to.dtype", (False,) * 5),
                ("aten::_to_copy.", (False,) * 7),
                ("aten::empty_strided.", (False,) * 6),
                #
                # copy_(Tensor(a!) self, Tensor src, bool non_blocking=False) -> Tensor(a!)
                ("aten::copy_.", (True, False, False)),
                ("aten::empty_like.", (False,) * 6),
                ("aten::empty_strided.", (False,) * 6),
                #
                # sin.out(Tensor self, *, Tensor(a!) out) -> Tensor(a!)
                ("aten::sin.out", (False, True)),
            ),
        )

    def test_match_schemas_backward(self) -> None:
        x = torch.ones((1,))
        w = torch.ones((1,), requires_grad=True)
        with profile() as prof:
            torch.mul(x, w).backward()

        self.assertEqual(
            self.formatSchemas(prof),
            (
                ("aten::mul.Tensor", (False, False)),
                ("aten::ones_like.", (False,) * 6),
                ("aten::empty_like.", (False,) * 6),
                ("aten::empty_strided.", (False,) * 6),
                #
                # fill_.Scalar(Tensor(a!) self, Scalar value) -> Tensor(a!)
                ("aten::fill_.Scalar", (True, False)),
                ("autograd::engine::evaluate_function: MulBackward0", ()),
                #
                # Cannot find schema, all inputs presumed mutable
                ("MulBackward0", (True,)),
                ("aten::mul.Tensor", (False, False)),
                (
                    "autograd::engine::evaluate_function: torch::autograd::AccumulateGrad",
                    (),
                ),
                #
                # Cannot find schema, all inputs presumed mutable
                ("torch::autograd::AccumulateGrad", (True,)),
                ("aten::detach.", (False,)),
                ("detach", (True,)),
            ),
        )

    def test_data_flow_graph_with_annotations(self) -> None:
        def f(x, y):
            # torch._C._jit_get_schemas_for_operator will reject any name that
            # is missing a namespace. (denoted by the presence of "::") We want
            # to check that we skip both annotations which have no schema
            # (return empty tuple from SchemaMatcher.lookup_schemas) and
            # annotations which cannot have schema (return None from
            # SchemaMatcher.lookup_schemas).
            with torch.profiler.record_function("Namespaced::Annotation"):
                with torch.profiler.record_function("My Annotation"):
                    x.zero_()
                    y.zero_()
                    return {"x0": torch.ones_like(x), "y0": torch.zeros_like(y)}

        # `record_function` makes a Tensor to hold its handle which is why we
        # see `aten::zeros` for `T0` and `T1`.
        inputs = {"x": torch.ones((1,)), "y": torch.ones((1,))}
        self.assertExpectedInline(
            self._run_and_format_data_flow(inputs, f),
            """\
            x:       T2
            y:       T3
            x0:      T4
            y0:      T5

            aten::zeros                                ->  T0(v0)
            [memory]                  T0(v0*)          ->
            aten::zeros                                ->  T1(v0)
            [memory]                  T1(v0*)          ->
            aten::zero_               T2(v0)           ->  T2(v1)
            aten::zero_               T3(v0)           ->  T3(v1)
            aten::ones_like           T2(v1)           ->  T4(v0)
            aten::zeros_like          T3(v1)           ->  T5(v0)""",
        )

    def test_data_flow_graph_non_op_allocations(self) -> None:
        def f(x):
            x.mul(2)

        # The python arg parser will convert the python scalar `2` to a Tensor
        # to pass to `aten::mul`. As a result there is no op that "owns" the
        # allocation. The Tensor deletions also do not happen in an op; they
        # are collected as a result of the Python objects going out of scope.
        self.assertExpectedInline(
            self._run_and_format_data_flow({"x": torch.ones((1,))}, f),
            """\
            x:       T1

            [memory]                                   ->  T0(v0)
            aten::mul                 T0(v0), T1(v0)   ->
            [memory]                  T0(v0*)          ->""",
        )

    def test_data_flow_graph_simple(self) -> None:
        inputs = {"x": torch.ones((25,)), "y": torch.ones((25,), requires_grad=True)}

        def f0(x, y):
            z = x.mul(y)
            return {"z": z.view_as(z)}

        def f1(x, y):
            with torch.no_grad():
                return f0(x, y)

        self.assertExpectedInline(
            self._run_and_format_data_flow(inputs, f0),
            """\
            x:       T0
            y:       T1
            z:       T2

            aten::mul                 T0(v0), T1(v0)   ->  T2(v0)
            aten::view_as             T2(v0)           ->""",
        )

        # Out of place is identical regardless of Autograd.
        self.assertExpectedInline(
            self._run_and_format_data_flow(inputs, f0),
            """\
            x:       T0
            y:       T1
            z:       T2

            aten::mul                 T0(v0), T1(v0)   ->  T2(v0)
            aten::view_as             T2(v0)           ->""",
        )

    def test_data_flow_graph_simple_inplace(self) -> None:
        inputs = {"x": torch.ones((25,)), "y": torch.ones((25,), requires_grad=True)}

        def f0(x, y):
            x.mul_(y)

        def f1(x, y):
            with torch.no_grad():
                return f0(x, y)

        # When Autograd is enabled a second Tensor `T2` is created to store
        # the values of T0(v0) which are needed for backwards.
        self.assertExpectedInline(
            self._run_and_format_data_flow(inputs, f0),
            """\
            x:       T0
            y:       T1

            aten::mul_                T0(v0), T1(v0)   ->  T0(v1), T2(v0)""",
        )

        self.assertExpectedInline(
            self._run_and_format_data_flow(inputs, f1),
            """\
            x:       T0
            y:       T1

            aten::mul_                T0(v0), T1(v0)   ->  T0(v1)""",
        )

    def test_data_flow_graph_simple_backward(self) -> None:
        inputs = {
            "x": torch.ones((1,)),
            "w": torch.ones((1,), requires_grad=True),
        }
        self.assertExpectedInline(
            self._run_and_format_data_flow(
                inputs, lambda x, w: (x * w).sin().backward()
            ),
            """\
            x:       T0
            w:       T1
            w.grad:   T7

            aten::mul                 T0(v0), T1(v0)   ->  T2(v0)
            aten::sin                 T2(v0)           ->  T3(v0)
            aten::ones_like           T3(v0)           ->  T4(v0)
            SinBackward0              T2(v0), T4(v0)   ->  T4(v1), T6(v0)
            [memory]                  T2(v0*)          ->
            MulBackward0              T0(v0), T6(v0)   ->  T6(v1), T7(v0)
            [memory]                  T6(v1*)          ->
            AccumulateGrad            T7(v0)           ->  T7(v1)
            [memory]                  T4(v1*)          ->
            [memory]                  T3(v0*)          ->""",
        )

    def test_data_flow_graph_complicated(self) -> None:
        def f():
            x = torch.ones((25,))
            y = x.mul(2).add_(2)
            z = torch.sin(y, out=torch.empty_like(y))
            return {"x": x, "y": y, "z": z}

        # T1 is the `2` in `.mul(2)`. The Python arg parser automatically
        # converts Scalar arguments to Tensors. The same is true for `T4`
        # and `.add_(2)`.
        self.assertExpectedInline(
            self._run_and_format_data_flow({}, f),
            """\
            x:       T0
            y:       T3
            z:       T6

            aten::ones                                 ->  T0(v0)
            [memory]                                   ->  T1(v0)
            aten::mul                 T0(v0), T1(v0)   ->  T3(v0)
            [memory]                  T1(v0*)          ->
            [memory]                                   ->  T4(v0)
            aten::add_                T3(v0), T4(v0)   ->  T3(v1)
            [memory]                  T4(v0*)          ->
            aten::empty_like          T3(v1)           ->  T6(v0)
            aten::sin                 T3(v1), T6(v0)   ->  T6(v1)""",
        )

        with profile() as prof:
            f()

        # `aten::mul` creates a temporary Tensor (T2), which is why the output
        # is has ID three rather than two.
        mul_node = prof._memory_profile()._data_flow_graph.flow_nodes[2]
        self.assertEqual(mul_node._event.name, "aten::mul")
        self.assertEqual(len(mul_node.intermediates), 1)
        self.assertEqual(mul_node.intermediates[0].id, 2)

    def test_data_flow_graph_stacked(self) -> None:
        inputs = {
            "x": torch.ones((25,)),
            "w0": torch.ones((1,), requires_grad=True),
            "w1": torch.ones((1,), requires_grad=True),
        }

        def f(x, w0, w1):
            return x.mul(w0).relu().mul(w1).relu().sum()

        def f_fwd(**kwargs):
            with torch.no_grad():
                return {"loss": f(**kwargs)}

        def f_fwd_bwd(**kwargs):
            loss = f(**kwargs)
            loss.backward()
            return {"loss": loss}

        self.assertExpectedInline(
            self._run_and_format_data_flow(inputs, f_fwd),
            """\
            x:       T0
            w0:      T1
            w1:      T4
            loss:    T7

            aten::mul                 T0(v0), T1(v0)   ->  T2(v0)
            aten::relu                T2(v0)           ->  T3(v0)
            [memory]                  T2(v0*)          ->
            aten::mul                 T3(v0), T4(v0)   ->  T5(v0)
            [memory]                  T3(v0*)          ->
            aten::relu                T5(v0)           ->  T6(v0)
            [memory]                  T5(v0*)          ->
            aten::sum                 T6(v0)           ->  T7(v0)
            [memory]                  T6(v0*)          ->""",
        )

        self.assertExpectedInline(
            self._run_and_format_data_flow(inputs, f_fwd_bwd),
            """\
            x:       T0
            w0:      T1
            w0.grad:  T15
            w1:      T4
            w1.grad:  T12
            loss:    T7

            aten::mul                 T0(v0), T1(v0)   ->  T2(v0)
            aten::relu                T2(v0)           ->  T3(v0)
            [memory]                  T2(v0*)          ->
            aten::mul                 T3(v0), T4(v0)   ->  T5(v0)
            aten::relu                T5(v0)           ->  T6(v0)
            [memory]                  T5(v0*)          ->
            aten::sum                 T6(v0)           ->  T7(v0)
            aten::ones_like           T7(v0)           ->  T8(v0)
            SumBackward0              T8(v0)           ->  T8(v1)
            ReluBackward0             T6(v0), T8(v1)   ->  T8(v2), T9(v0)
            [memory]                  T6(v0*)          ->
            MulBackward0              T3(v0), T4(v0), T9(v0)  ->  T9(v1), T10(v0), T11(v0)
            aten::sum                 T10(v0)          ->  T12(v0)
            [memory]                  T10(v0*)         ->
            [memory]                  T9(v1*)          ->
            AccumulateGrad            T12(v0)          ->  T12(v1)
            ReluBackward0             T3(v0), T11(v0)  ->  T11(v1), T13(v0)
            [memory]                  T11(v1*)         ->
            [memory]                  T3(v0*)          ->
            MulBackward0              T0(v0), T13(v0)  ->  T13(v1), T14(v0)
            aten::sum                 T14(v0)          ->  T15(v0)
            [memory]                  T14(v0*)         ->
            [memory]                  T13(v1*)         ->
            AccumulateGrad            T15(v0)          ->  T15(v1)
            [memory]                  T8(v2*)          ->""",
        )

        # Second time grads are already initialized.
        self.assertExpectedInline(
            self._run_and_format_data_flow(inputs, f_fwd_bwd),
            """\
            x:       T0
            w0:      T1
            w0.grad:  T17
            w1:      T4
            w1.grad:  T13
            loss:    T7

            aten::mul                 T0(v0), T1(v0)   ->  T2(v0)
            aten::relu                T2(v0)           ->  T3(v0)
            [memory]                  T2(v0*)          ->
            aten::mul                 T3(v0), T4(v0)   ->  T5(v0)
            aten::relu                T5(v0)           ->  T6(v0)
            [memory]                  T5(v0*)          ->
            aten::sum                 T6(v0)           ->  T7(v0)
            aten::ones_like           T7(v0)           ->  T8(v0)
            SumBackward0              T8(v0)           ->  T8(v1)
            ReluBackward0             T6(v0), T8(v1)   ->  T8(v2), T9(v0)
            [memory]                  T6(v0*)          ->
            MulBackward0              T3(v0), T4(v0), T9(v0)  ->  T9(v1), T10(v0), T11(v0)
            aten::sum                 T10(v0)          ->  T12(v0)
            [memory]                  T10(v0*)         ->
            [memory]                  T9(v1*)          ->
            AccumulateGrad            T12(v0*), T13(v0)  ->  T13(v1)
            ReluBackward0             T3(v0), T11(v0)  ->  T11(v1), T14(v0)
            [memory]                  T11(v1*)         ->
            [memory]                  T3(v0*)          ->
            MulBackward0              T0(v0), T14(v0)  ->  T14(v1), T15(v0)
            aten::sum                 T15(v0)          ->  T16(v0)
            [memory]                  T15(v0*)         ->
            [memory]                  T14(v1*)         ->
            AccumulateGrad            T16(v0*), T17(v0)  ->  T17(v1)
            [memory]                  T8(v2*)          ->""",
        )

        return

        x = torch.ones((25,))
        w0 = torch.ones((1,), requires_grad=True)
        w1 = torch.ones((1,), requires_grad=True)

        with profile() as prof_no_grad:
            with torch.no_grad():
                x.mul(w0).relu().mul(w1).relu().sum()

        # TODO: one with `.logsumexp(dim=0)`

        self.assertExpectedInline(
            self._format_graph(prof_no_grad),
            """\
            aten::mul                 T0(v0), T1(v0)   ->  T2(v0)
            aten::relu                T2(v0)           ->  T3(v0)
            [memory]                  T2(v0*)          ->
            aten::mul                 T3(v0), T4(v0)   ->  T5(v0)
            [memory]                  T3(v0*)          ->
            aten::relu                T5(v0)           ->  T6(v0)
            [memory]                  T5(v0*)          ->
            aten::sum                 T6(v0)           ->  T7(v0)
            [memory]                  T6(v0*)          ->
            [memory]                  T7(v0*)          ->""",
        )

        with profile() as prof_grad:
            loss = x.mul(w0).relu().mul(w1).relu().sum()
            loss.backward()

        self.assertExpectedInline(
            self._format_graph(prof_grad),
            """\
            aten::mul                 T0(v0), T1(v0)   ->  T2(v0)
            aten::relu                T2(v0)           ->  T3(v0)
            [memory]                  T2(v0*)          ->
            aten::mul                 T3(v0), T4(v0)   ->  T5(v0)
            aten::relu                T5(v0)           ->  T6(v0)
            [memory]                  T5(v0*)          ->
            aten::sum                 T6(v0)           ->  T7(v0)
            aten::ones_like           T7(v0)           ->  T8(v0)
            SumBackward0              T8(v0)           ->  T8(v1)
            ReluBackward0             T6(v0), T8(v1)   ->  T8(v2), T9(v0)
            [memory]                  T6(v0*)          ->
            MulBackward0              T3(v0), T4(v0), T9(v0)  ->  T9(v1), T10(v0), T11(v0)
            aten::sum                 T10(v0)          ->  T12(v0)
            [memory]                  T10(v0*)         ->
            [memory]                  T9(v1*)          ->
            AccumulateGrad            T12(v0)          ->  T12(v1)
            ReluBackward0             T3(v0), T11(v0)  ->  T11(v1), T13(v0)
            [memory]                  T11(v1*)         ->
            [memory]                  T3(v0*)          ->
            MulBackward0              T0(v0), T13(v0)  ->  T13(v1), T14(v0)
            aten::sum                 T14(v0)          ->  T15(v0)
            [memory]                  T14(v0*)         ->
            [memory]                  T13(v1*)         ->
            AccumulateGrad            T15(v0)          ->  T15(v1)
            [memory]                  T8(v2*)          ->""",
        )

        # Second time grads are already initialized.
        with profile() as prof_grad:
            loss = x.mul(w0).relu().mul(w1).relu().sum()
            loss.backward()

        self.assertExpectedInline(
            self._format_graph(prof_grad),
            """\
            aten::mul                 T0(v0), T1(v0)   ->  T2(v0)
            aten::relu                T2(v0)           ->  T3(v0)
            [memory]                  T2(v0*)          ->
            aten::mul                 T3(v0), T4(v0)   ->  T5(v0)
            aten::relu                T5(v0)           ->  T6(v0)
            [memory]                  T5(v0*)          ->
            aten::sum                 T6(v0)           ->  T7(v0)
            aten::ones_like           T7(v0)           ->  T8(v0)
            SumBackward0              T8(v0)           ->  T8(v1)
            ReluBackward0             T6(v0), T8(v1)   ->  T8(v2), T9(v0)
            [memory]                  T6(v0*)          ->
            MulBackward0              T3(v0), T4(v0), T9(v0)  ->  T9(v1), T10(v0), T11(v0)
            aten::sum                 T10(v0)          ->  T12(v0)
            [memory]                  T10(v0*)         ->
            [memory]                  T9(v1*)          ->
            AccumulateGrad            T12(v0*), T13(v0)  ->  T13(v1)
            ReluBackward0             T3(v0), T11(v0)  ->  T11(v1), T14(v0)
            [memory]                  T11(v1*)         ->
            [memory]                  T3(v0*)          ->
            MulBackward0              T0(v0), T14(v0)  ->  T14(v1), T15(v0)
            aten::sum                 T15(v0)          ->  T16(v0)
            [memory]                  T15(v0*)         ->
            [memory]                  T14(v1*)         ->
            AccumulateGrad            T16(v0*), T17(v0)  ->  T17(v1)
            [memory]                  T8(v2*)          ->""",
        )


if __name__ == "__main__":
    run_tests()
