"""Typed, secret-safe exceptions for the YouTube integration."""


class YouTubeError(RuntimeError):
    """Base error for YouTube operations."""


class YouTubeConfigurationError(YouTubeError):
    """The local YouTube configuration or keystore is invalid."""


class YouTubeCredentialsError(YouTubeError):
    """Stored credentials are missing, invalid or need reauthorization."""


class YouTubeAuthenticationError(YouTubeCredentialsError):
    """An OAuth authorization or token refresh failed."""


class YouTubeValidationError(YouTubeError):
    """The requested YouTube publication is invalid."""


class YouTubeAPIError(YouTubeError):
    """YouTube returned an API error."""


class YouTubeUploadError(YouTubeAPIError):
    """A resumable video upload failed."""


class YouTubeAmbiguousUploadError(YouTubeUploadError):
    """An interrupted upload may already have created a video."""


class YouTubeThumbnailError(YouTubeAPIError):
    """Uploading a thumbnail failed without invalidating the video."""
