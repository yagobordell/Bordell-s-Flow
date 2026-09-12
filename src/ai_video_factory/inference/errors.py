class InferenceInfrastructureError(RuntimeError):
    """Base error for remote inference infrastructure."""


class ModelBootstrapPendingError(InferenceInfrastructureError):
    """Required model bootstrap artifacts are not available yet."""


class JobConflictError(InferenceInfrastructureError):
    """The same application job ID was reused for different immutable input."""


class OutputConflictError(JobConflictError):
    """The deterministic output key exists but belongs to a different request."""


class JobBusyError(InferenceInfrastructureError):
    """Another worker still owns a live lease for this job."""


class LeaseLostError(InferenceInfrastructureError):
    """The worker no longer owns the lease and must not commit an output."""


class InputIntegrityError(InferenceInfrastructureError):
    """A downloaded input does not match its declared digest."""


class UnsupportedTaskError(InferenceInfrastructureError):
    """No task runner is registered for the requested task."""


class JobExecutionError(InferenceInfrastructureError):
    """A retryable infrastructure or task execution failure occurred."""
