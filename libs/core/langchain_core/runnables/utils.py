"""Utility code for runnables."""
from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from inspect import signature
from itertools import groupby
from typing import (
    Any,
    AsyncIterable,
    AsyncIterator,
    Callable,
    Coroutine,
    Dict,
    Iterable,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Protocol,
    Sequence,
    Set,
    TypeVar,
    Union,
)

from typing_extensions import TypedDict

from langchain_core.tracers import RunLog, RunLogPatch
from typing_extensions import NotRequired

Input = TypeVar("Input", contravariant=True)
# Output type should implement __concat__, as eg str, list, dict do
Output = TypeVar("Output", covariant=True)


async def gated_coro(semaphore: asyncio.Semaphore, coro: Coroutine) -> Any:
    """Run a coroutine with a semaphore.
    Args:
        semaphore: The semaphore to use.
        coro: The coroutine to run.

    Returns:
        The result of the coroutine.
    """
    async with semaphore:
        return await coro


async def gather_with_concurrency(n: Union[int, None], *coros: Coroutine) -> list:
    """Gather coroutines with a limit on the number of concurrent coroutines."""
    if n is None:
        return await asyncio.gather(*coros)

    semaphore = asyncio.Semaphore(n)

    return await asyncio.gather(*(gated_coro(semaphore, c) for c in coros))


def accepts_run_manager(callable: Callable[..., Any]) -> bool:
    """Check if a callable accepts a run_manager argument."""
    try:
        return signature(callable).parameters.get("run_manager") is not None
    except ValueError:
        return False


def accepts_config(callable: Callable[..., Any]) -> bool:
    """Check if a callable accepts a config argument."""
    try:
        return signature(callable).parameters.get("config") is not None
    except ValueError:
        return False


def accepts_context(callable: Callable[..., Any]) -> bool:
    """Check if a callable accepts a context argument."""
    try:
        return signature(callable).parameters.get("context") is not None
    except ValueError:
        return False


class IsLocalDict(ast.NodeVisitor):
    """Check if a name is a local dict."""

    def __init__(self, name: str, keys: Set[str]) -> None:
        self.name = name
        self.keys = keys

    def visit_Subscript(self, node: ast.Subscript) -> Any:
        if (
            isinstance(node.ctx, ast.Load)
            and isinstance(node.value, ast.Name)
            and node.value.id == self.name
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            # we've found a subscript access on the name we're looking for
            self.keys.add(node.slice.value)

    def visit_Call(self, node: ast.Call) -> Any:
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == self.name
            and node.func.attr == "get"
            and len(node.args) in (1, 2)
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            # we've found a .get() call on the name we're looking for
            self.keys.add(node.args[0].value)


class IsFunctionArgDict(ast.NodeVisitor):
    """Check if the first argument of a function is a dict."""

    def __init__(self) -> None:
        self.keys: Set[str] = set()

    def visit_Lambda(self, node: ast.Lambda) -> Any:
        if not node.args.args:
            return
        input_arg_name = node.args.args[0].arg
        IsLocalDict(input_arg_name, self.keys).visit(node.body)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        if not node.args.args:
            return
        input_arg_name = node.args.args[0].arg
        IsLocalDict(input_arg_name, self.keys).visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        if not node.args.args:
            return
        input_arg_name = node.args.args[0].arg
        IsLocalDict(input_arg_name, self.keys).visit(node)


class NonLocals(ast.NodeVisitor):
    """Get nonlocal variables accessed."""

    def __init__(self) -> None:
        self.loads: Set[str] = set()
        self.stores: Set[str] = set()

    def visit_Name(self, node: ast.Name) -> Any:
        if isinstance(node.ctx, ast.Load):
            self.loads.add(node.id)
        elif isinstance(node.ctx, ast.Store):
            self.stores.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        if isinstance(node.ctx, ast.Load):
            parent = node.value
            attr_expr = node.attr
            while isinstance(parent, ast.Attribute):
                attr_expr = parent.attr + "." + attr_expr
                parent = parent.value
            if isinstance(parent, ast.Name):
                self.loads.add(parent.id + "." + attr_expr)
                self.loads.discard(parent.id)


class FunctionNonLocals(ast.NodeVisitor):
    """Get the nonlocal variables accessed of a function."""

    def __init__(self) -> None:
        self.nonlocals: Set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        visitor = NonLocals()
        visitor.visit(node)
        self.nonlocals.update(visitor.loads - visitor.stores)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        visitor = NonLocals()
        visitor.visit(node)
        self.nonlocals.update(visitor.loads - visitor.stores)

    def visit_Lambda(self, node: ast.Lambda) -> Any:
        visitor = NonLocals()
        visitor.visit(node)
        self.nonlocals.update(visitor.loads - visitor.stores)


class GetLambdaSource(ast.NodeVisitor):
    """Get the source code of a lambda function."""

    def __init__(self) -> None:
        """Initialize the visitor."""
        self.source: Optional[str] = None
        self.count = 0

    def visit_Lambda(self, node: ast.Lambda) -> Any:
        """Visit a lambda function."""
        self.count += 1
        if hasattr(ast, "unparse"):
            self.source = ast.unparse(node)


def get_function_first_arg_dict_keys(func: Callable) -> Optional[List[str]]:
    """Get the keys of the first argument of a function if it is a dict."""
    try:
        code = inspect.getsource(func)
        tree = ast.parse(textwrap.dedent(code))
        visitor = IsFunctionArgDict()
        visitor.visit(tree)
        return list(visitor.keys) if visitor.keys else None
    except (SyntaxError, TypeError, OSError):
        return None


def get_lambda_source(func: Callable) -> Optional[str]:
    """Get the source code of a lambda function.

    Args:
        func: a callable that can be a lambda function

    Returns:
        str: the source code of the lambda function
    """
    try:
        name = func.__name__ if func.__name__ != "<lambda>" else None
    except AttributeError:
        name = None
    try:
        code = inspect.getsource(func)
        tree = ast.parse(textwrap.dedent(code))
        visitor = GetLambdaSource()
        visitor.visit(tree)
        return visitor.source if visitor.count == 1 else name
    except (SyntaxError, TypeError, OSError):
        return name


def get_function_nonlocals(func: Callable) -> List[Any]:
    """Get the nonlocal variables accessed by a function."""
    try:
        code = inspect.getsource(func)
        tree = ast.parse(textwrap.dedent(code))
        visitor = FunctionNonLocals()
        visitor.visit(tree)
        values: List[Any] = []
        for k, v in inspect.getclosurevars(func).nonlocals.items():
            if k in visitor.nonlocals:
                values.append(v)
            for kk in visitor.nonlocals:
                if "." in kk and kk.startswith(k):
                    vv = v
                    for part in kk.split(".")[1:]:
                        vv = getattr(vv, part)
                    values.append(vv)
        return values
    except (SyntaxError, TypeError, OSError):
        return []


def indent_lines_after_first(text: str, prefix: str) -> str:
    """Indent all lines of text after the first line.

    Args:
        text:  The text to indent
        prefix: Used to determine the number of spaces to indent

    Returns:
        str: The indented text
    """
    n_spaces = len(prefix)
    spaces = " " * n_spaces
    lines = text.splitlines()
    return "\n".join([lines[0]] + [spaces + line for line in lines[1:]])


class AddableDict(Dict[str, Any]):
    """
    Dictionary that can be added to another dictionary.
    """

    def __add__(self, other: AddableDict) -> AddableDict:
        chunk = AddableDict(self)
        for key in other:
            if key not in chunk or chunk[key] is None:
                chunk[key] = other[key]
            elif other[key] is not None:
                try:
                    added = chunk[key] + other[key]
                except TypeError:
                    added = other[key]
                chunk[key] = added
        return chunk

    def __radd__(self, other: AddableDict) -> AddableDict:
        chunk = AddableDict(other)
        for key in self:
            if key not in chunk or chunk[key] is None:
                chunk[key] = self[key]
            elif self[key] is not None:
                try:
                    added = chunk[key] + self[key]
                except TypeError:
                    added = self[key]
                chunk[key] = added
        return chunk


_T_co = TypeVar("_T_co", covariant=True)
_T_contra = TypeVar("_T_contra", contravariant=True)


class SupportsAdd(Protocol[_T_contra, _T_co]):
    """Protocol for objects that support addition."""

    def __add__(self, __x: _T_contra) -> _T_co:
        ...


Addable = TypeVar("Addable", bound=SupportsAdd[Any, Any])


def add(addables: Iterable[Addable]) -> Optional[Addable]:
    """Add a sequence of addable objects together."""
    final = None
    for chunk in addables:
        if final is None:
            final = chunk
        else:
            final = final + chunk
    return final


async def aadd(addables: AsyncIterable[Addable]) -> Optional[Addable]:
    """Asynchronously add a sequence of addable objects together."""
    final = None
    async for chunk in addables:
        if final is None:
            final = chunk
        else:
            final = final + chunk
    return final


class ConfigurableField(NamedTuple):
    """A field that can be configured by the user."""

    id: str

    name: Optional[str] = None
    description: Optional[str] = None
    annotation: Optional[Any] = None
    is_shared: bool = False

    def __hash__(self) -> int:
        return hash((self.id, self.annotation))


class ConfigurableFieldSingleOption(NamedTuple):
    """A field that can be configured by the user with a default value."""

    id: str
    options: Mapping[str, Any]
    default: str

    name: Optional[str] = None
    description: Optional[str] = None
    is_shared: bool = False

    def __hash__(self) -> int:
        return hash((self.id, tuple(self.options.keys()), self.default))


class ConfigurableFieldMultiOption(NamedTuple):
    """A field that can be configured by the user with multiple default values."""

    id: str
    options: Mapping[str, Any]
    default: Sequence[str]

    name: Optional[str] = None
    description: Optional[str] = None
    is_shared: bool = False

    def __hash__(self) -> int:
        return hash((self.id, tuple(self.options.keys()), tuple(self.default)))


AnyConfigurableField = Union[
    ConfigurableField, ConfigurableFieldSingleOption, ConfigurableFieldMultiOption
]


class ConfigurableFieldSpec(NamedTuple):
    """A field that can be configured by the user. It is a specification of a field."""

    id: str
    annotation: Any

    name: Optional[str] = None
    description: Optional[str] = None
    default: Any = None
    is_shared: bool = False
    dependencies: Optional[List[str]] = None


def get_unique_config_specs(
    specs: Iterable[ConfigurableFieldSpec],
) -> List[ConfigurableFieldSpec]:
    """Get the unique config specs from a sequence of config specs."""
    grouped = groupby(
        sorted(specs, key=lambda s: (s.id, *(s.dependencies or []))), lambda s: s.id
    )
    unique: List[ConfigurableFieldSpec] = []
    for id, dupes in grouped:
        first = next(dupes)
        others = list(dupes)
        if len(others) == 0:
            unique.append(first)
        elif all(o == first for o in others):
            unique.append(first)
        else:
            raise ValueError(
                "RunnableSequence contains conflicting config specs"
                f"for {id}: {[first] + others}"
            )
    return unique


class StreamEvent(TypedDict):
    """TODO: Document me."""

    event: str
    """The event type."""
    name: str
    """The name of the runnable that generated the event."""
    run_id: str
    """The run id."""
    tags: NotRequired[List[str]]
    """The tags."""
    metadata: NotRequired[Dict[str, Any]]
    """The metadata."""
    data: Any
    """Event data.

    The contents of the event data depend on the event type.
    """


async def as_event_stream(
    run_log_patches: AsyncIterator[RunLogPatch]
) -> AsyncIterator[StreamEvent]:
    """Convert a stream of run log patches to a stream of events.

    This is a utility function that can be used to convert the output of a runnable's
    astream_log method to a stream of events that should be easier to work with.

    Example:

        .. code-block:: python

    Args:
        run_log_patches: The output from astream_log method of a runnable.

    Returns:
        An async stream of events.
    """
    run_log = RunLog(state=None)  # type: ignore[arg-type]
    yielded_start_event = False

    async for log in run_log_patches:
        run_log = run_log + log

        if not yielded_start_event:
            state = run_log.state.copy()
            # TODO(FIX): Start event still does not capture inputs.
            # We could either assume the client already has this information,
            # or else propagate some information to the start event
            # _atransform_stream_with_config, we need to propagate the inputs
            # if state["type"] == "chain":
            #     data = state["inputs"]["input"]
            # else:
            #     data = state["inputs"]
            # For now, we'll just set the data to an empty dict
            data = {}
            if "id" in state:
                yield StreamEvent(
                    event=f"on_{state['type']}_start",
                    name=state["name"],
                    run_id=state["id"],
                    tags=[],
                    metadata={},
                    data=data,
                )
                yielded_start_event = True

        paths = {
            op["path"].split("/")[2]
            for op in log.ops
            if op["path"].startswith("/logs/")
        }

        # TODO iteration here is in the same order
        for path in paths:
            data = {}
            log = run_log.state["logs"][path]
            if log["end_time"] is None:
                if log["streamed_output"]:
                    event_type = "stream"
                else:
                    event_type = "start"
            # elif log["error"] is not None:
            #     event_type = "error"
            else:
                event_type = "end"

            if event_type == "start":
                # Propagate the inputs to the start event if they are available
                # They will usually NOT be available for components that are able
                # to operate on a stream since the input value won't be known
                # until the end of the stream.
                # Old style
                if log["type"] in {"retriever", "tool", "llm"}:
                    if log["inputs"]:
                        data["input"] = log["inputs"]
                        # Clean up the inputs since we don't need them anymore
                        # del log["inputs"]
                else:  # new style chains
                    data["input"] = log["inputs"]["input"]
                    # Clean up the inputs since we don't need them anymore
                    # del log["inputs"]

            if event_type == "end":
                # Adapter for old style chains
                if log["type"] in {"retriever", "tool", "llm"}:
                    data["output"] = log["final_output"]
                    # Clean up the final output since we don't need it anymore
                    del log["final_output"]
                    # For runnables that implementing streaming, the input to
                    # the runnable may be available at this stage, if it is
                    # then we'll add them to the event data.
                    if log["inputs"]:
                        data["input"] = log["inputs"]
                        del log["inputs"]
                else:  # New style chains
                    final_output = log["final_output"]
                    if final_output is None:
                        data["output"] = None
                    elif isinstance(final_output, dict):
                        data["output"] = final_output.get("output", None)
                        # Clean up the final output since we don't need it anymore
                        del log["final_output"]
                    else:
                        # Ignore unrecognized final output type
                        pass
                    # For runnables that implementing streaming, the input to
                    # the runnable may be available at this stage, if it is
                    # then we'll add them to the event data.
                    if log["inputs"]:
                        data["input"] = log["inputs"]["input"]
                        del log["inputs"]

            if event_type == "stream":
                num_chunks = len(log["streamed_output"])
                if num_chunks != 1:
                    raise AssertionError(
                        f"Expected exactly one chunk of streamed output, got {num_chunks}"
                        f" instead. This is impossible. Encountered in: {log['name']}"
                    )

                data = {"chunk": log["streamed_output"][0]}
                # Clean up the stream, we don't need it anymore.
                # And this avoids duplicates as well!
                log["streamed_output"] = []

            yield StreamEvent(
                event=f"on_{log['type']}_{event_type}",
                name=log["name"],
                run_id=log["id"],
                tags=log["tags"],
                metadata=log["metadata"],
                data=data,
            )

        state = run_log.state
        if state["streamed_output"]:
            num_chunks = len(state["streamed_output"])
            if num_chunks != 1:
                raise AssertionError(
                    f"Expected exactly one chunk of streamed output, got {num_chunks}"
                    f" instead. This is impossible. Encountered in: {state['name']}"
                )

            data = {"chunk": state["streamed_output"][0]}
            # Clean up the stream, we don't need it anymore.
            state["streamed_output"] = []

            yield StreamEvent(
                event=f"on_{state['type']}_stream",  # TODO: fix this
                name=state["name"],
                run_id=state["id"],
                tags=state.get("tags", []),
                metadata=state.get("metadata", {}),
                data=data,
            )

    state = run_log.state.copy()
    state.update(
        {
            "tags": [],
            "metadata": {},
        }
    )

    data = {"output": state["final_output"]}

    yield StreamEvent(
        event=f"on_{state['type']}_end",  # TODO: fix this
        name=state["name"],
        run_id=state["id"],
        tags=state["tags"],
        metadata=state["metadata"],
        data=data,
    )
