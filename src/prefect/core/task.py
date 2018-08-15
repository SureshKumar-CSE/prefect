import inspect
import warnings
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, Tuple

import prefect
import prefect.engine.cache_validators
import prefect.engine.signals
import prefect.triggers
from prefect.utilities.json import Serializable, to_qualified_name

if TYPE_CHECKING:
    from prefect.core.flow import Flow  # pylint: disable=W0611
    from prefect.engine.state import State

VAR_KEYWORD = inspect.Parameter.VAR_KEYWORD


class SignatureValidator(type):
    def __new__(cls, name, parents, methods):
        run = methods.get("run", lambda: None)
        run_sig = inspect.getfullargspec(run)
        if run_sig.varargs:
            raise ValueError(
                "Tasks with variable positional arguments (*args) are not "
                "supported, because all Prefect arguments are stored as "
                "keywords. As a workaround, consider modifying the run() "
                "method to accept **kwargs and feeding the values "
                "to *args."
            )
        # necessary to ensure classes that inherit from parent class
        # also get passed through __new__
        return type.__new__(cls, name, parents, methods)


class Task(Serializable, metaclass=SignatureValidator):
    """
    The Task class which is used as the full representation of a unit of work.

    This Task class can be used directly as a first class object where it must
    be inherited from by a class which implements the `run` method.  For a more
    functional way of generating Tasks, see [the task decorator](../utilities/tasks.html).

    Inheritance example:
    ```python
    class AddTask(Task):
        def run(self, x, y):
            return x + y
    ```

    *Note:* The implemented `run` method cannot have `*args` in its signature.

    Args:
        - name (str, optional): The name of this task
        - slug (str, optional): The slug for this task, it must be unique withing a given Flow
        - description (str, optional): Descriptive information about this task
        - group (str, optional): Group in which this task belongs to
        - tags ([str], optional): A list of tags for this task
        - max_retries (int, optional): The maximum amount of times this task can be retried
        - retry_delay (timedelta, optional): The amount of time to wait until task is retried
        - timeout (timedelta, optional): The amount of time to wait while running before a timeout occurs
        - trigger (callable, optional): a function that determines whether the task should run, based
                on the states of any upstream tasks.
        - skip_on_upstream_skip (bool, optional): if True and any upstream tasks skipped, this task
                will automatically be skipped as well. By default, this prevents tasks from
                attempting to use either state or data from tasks that didn't run. if False,
                the task's trigger will be called as normal; skips are considered successes.
        - cache_for (timedelta, optional): The amount of time to maintain cache
        - cache_validator (Callable, optional): Validator telling what to cache

    Raises:
        - TypeError: if `tags` is of type `str`
    """

    def __init__(
        self,
        name: str = None,
        slug: str = None,
        description: str = None,
        group: str = None,
        tags: Iterable[str] = None,
        max_retries: int = 0,
        retry_delay: timedelta = timedelta(minutes=1),
        timeout: timedelta = None,
        trigger: Callable[[Dict["Task", "State"]], bool] = None,
        skip_on_upstream_skip: bool = True,
        cache_for: timedelta = None,
        cache_validator: Callable = None,
    ) -> None:
        self.name = name or type(self).__name__
        self.slug = slug
        self.description = description

        self.group = str(group or prefect.context.get("_group", ""))

        # avoid silently iterating over a string
        if isinstance(tags, str):
            raise TypeError("Tags should be a set of tags, not a string.")
        self.tags = set(tags or prefect.context.get("_tags", []))

        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.timeout = timeout

        self.trigger = trigger or prefect.triggers.all_successful
        self.skip_on_upstream_skip = skip_on_upstream_skip

        if cache_for is None and cache_validator is not None:
            warnings.warn(
                "cache_validator provided without specifying cache expiration (cache_for); this Task will not be cached."
            )

        self.cache_for = cache_for
        default_validator = (
            prefect.engine.cache_validators.never_use
            if cache_for is None
            else prefect.engine.cache_validators.duration_only
        )
        self.cache_validator = cache_validator or default_validator

    def __repr__(self) -> str:
        return "<Task: {self.name}>".format(self=self)

    # Run  --------------------------------------------------------------------

    def inputs(self) -> Tuple[str, ...]:
        """
        Get the inputs for this task

        Returns:
            - tuple of strings representing the inputs for this task
        """
        return tuple(inspect.signature(self.run).parameters.keys())

    def run(self):  # type: ignore
        """
        The main entrypoint for tasks.

        In addition to running arbitrary functions, tasks can interact with
        Prefect in a few ways:
            1. Return an optional result. When this function runs successfully,
                the task is considered successful and the result (if any) is
                made available to downstream edges.
            2. Raise an error. Errors are interpreted as failure.
            3. Raise a signal. Signals can include `FAIL`, `SUCCESS`, `WAIT`, etc.
                and indicate that the task should be put in the indicated
                state.
                - `FAIL` will lead to retries if appropriate
                - `WAIT` will end execution and skip all downstream tasks with
                    state WAITING_FOR_UPSTREAM (unless appropriate triggers
                    are set). The task can be run again and should check
                    context.is_waiting to see if it was placed in a WAIT.
        """
        raise NotImplementedError()

    # Dependencies -------------------------------------------------------------

    def __call__(
        self, *args: object, upstream_tasks: Iterable[object] = None, **kwargs: object
    ) -> "Task":
        # this will raise an error if callargs weren't all provided
        signature = inspect.signature(self.run)
        callargs = dict(signature.bind(*args, **kwargs).arguments)  # type: Dict

        # bind() compresses all variable keyword arguments under the ** argument name,
        # so we expand them explicitly
        var_kw_arg = next(
            (p for p in signature.parameters.values() if p.kind == VAR_KEYWORD), None
        )
        callargs.update(callargs.pop(var_kw_arg, {}))

        flow = prefect.context.get("_flow", None)
        if not flow:
            raise ValueError("Could not infer an active Flow context.")

        self.set_dependencies(
            flow=flow, upstream_tasks=upstream_tasks, keyword_tasks=callargs
        )

        return self

    def set_dependencies(
        self,
        flow: "Flow" = None,
        upstream_tasks: Iterable[object] = None,
        downstream_tasks: Iterable[object] = None,
        keyword_tasks: Dict[str, object] = None,
        validate: bool = True,
    ) -> None:
        """
        Set dependencies for a flow either specified or in the current context using this task

        Args:
            - flow (Flow, optional): The flow to set dependencies on, defaults to the current
            flow in context if no flow is specified
            - upstream_tasks ([object], optional): A list of upstream tasks for this task
            - downstream_tasks ([object], optional): A list of downtream tasks for this task
            - keyword_tasks ({str, object}}, optional): The results of these tasks will be provided
            to the task under the specified keyword arguments.
            - validate (bool, optional): Whether or not to check the validity of the flow

        Returns:
            - None

        Raises:
            - ValueError: if no flow is specified and no flow can be found in the current context
        """
        flow = flow or prefect.context.get("_flow", None)
        if not flow:
            raise ValueError(
                "No Flow was passed, and could not infer an active Flow context."
            )

        flow.set_dependencies(  # type: ignore
            task=self,
            upstream_tasks=upstream_tasks,
            downstream_tasks=downstream_tasks,
            keyword_tasks=keyword_tasks,
            validate=validate,
        )

    # Serialization ------------------------------------------------------------

    def serialize(self) -> Dict[str, Any]:
        """
        Creates a serialized representation of this task

        Returns:
            - dict representing this task
        """
        return dict(
            name=self.name,
            slug=self.slug,
            description=self.description,
            group=self.group,
            tags=self.tags,
            type=to_qualified_name(type(self)),
            max_retries=self.max_retries,
            retry_delay=self.retry_delay,
            timeout=self.timeout,
            trigger=self.trigger,
            skip_on_upstream_skip=self.skip_on_upstream_skip,
            cache_for=self.cache_for,
            cache_validator=self.cache_validator,
        )


class Parameter(Task):
    """
    A Parameter is a special task that defines a required flow input.

    A parameter's "slug" is automatically -- and immutably -- set to the parameter name.
    Flows enforce slug uniqueness across all tasks, so this ensures that the flow has
    no other parameters by the same name.

    Args:
        - name (str): the Parameter name.
        - required (bool, optional): If True, the Parameter is required and the default
            value is ignored.
        - default (any, optional): A default value for the parameter. If the default
            is not None, the Parameter will not be required.
    """

    def __init__(self, name: str, default: Any = None, required: bool = True) -> None:
        if default is not None:
            required = False

        self.required = required
        self.default = default

        super().__init__(name=name, slug=name)

    def __repr__(self) -> str:
        return "<Parameter: {self.name}>".format(self=self)

    @property  # type: ignore
    def name(self) -> str:  # type: ignore
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        if hasattr(self, "_name"):
            raise AttributeError("Parameter name can not be changed")
        self._name = value  # pylint: disable=W0201

    @property  # type: ignore
    def slug(self) -> str:  # type: ignore
        """
        A Parameter slug is always the same as its name. This information is used by
        Flow objects to enforce parameter name uniqueness.
        """
        return self.name

    @slug.setter
    def slug(self, value: str) -> None:
        # slug is a property, so it's not actually set by this method, but the superclass
        # attempts to set it and we need to allow that without error.
        if value != self.name:
            raise AttributeError("Parameter slug must be the same as its name.")

    def run(self) -> Any:
        params = prefect.context.get("_parameters", {})
        if self.required and self.name not in params:
            raise prefect.engine.signals.FAIL(
                'Parameter "{}" was required but not provided.'.format(self.name)
            )
        return params.get(self.name, self.default)

    def info(self) -> Dict[str, Any]:
        info = super().info()
        info.update(required=self.required, default=self.default)
        return info