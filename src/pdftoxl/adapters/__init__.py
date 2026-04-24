"""External-package adapters.

Kept outside `pipeline_v1/` so pipeline stages depend on thin interfaces,
not on third-party SDKs directly. Swap an implementation here to re-point
the pipeline at a different backend without touching stage code.
"""
