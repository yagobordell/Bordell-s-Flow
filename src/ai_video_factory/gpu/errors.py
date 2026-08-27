class GPUInfrastructureError(RuntimeError):
    """Base error for the Phase 7 worker boundary."""


class JobConflictError(GPUInfrastructureError):
    """The same application job ID was reused for different immutable input."""


class OutputConflictError(JobConflictError):
    """The deterministic output key exists but belongs to a different request."""


class JobBusyError(GPUInfrastructureError):
    """Another worker still owns a live lease for this job."""


class LeaseLostError(GPUInfrastructureError):
    """The worker no longer owns the lease and must not commit an output."""


class InputIntegrityError(GPUInfrastructureError):
    """A downloaded input does not match its declared digest."""


class UnsupportedTaskError(GPUInfrastructureError):
    """No task runner is registered for the requested task."""


class JobExecutionError(GPUInfrastructureError):
    """A retryable infrastructure or task execution failure occurred."""
