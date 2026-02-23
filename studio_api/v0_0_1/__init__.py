from studio_api.v0_0_2 import (
    StudioAPIAdapter_v0_0_2,
    Input as Input_v0_0_2,
    InputImpl as InputImpl_v0_0_2,
    Types as Types_v0_0_2,
)
from typing import Type, TYPE_CHECKING
from studio_api.internal.async_to_sync import create_sync_class


# This version only exists to serve as a template for future version adapters.
# There is no reason anyone should ever use it.
class StudioAPIAdapter_v0_0_1(StudioAPIAdapter_v0_0_2):
    VERSION = "0.0.1"
    STABLE = True

class Input(Input_v0_0_2):
    pass

class InputImpl(InputImpl_v0_0_2):
    pass

class Types(Types_v0_0_2):
    pass

StudioAPI = StudioAPIAdapter_v0_0_1

# Create a synchronous version of the API
if TYPE_CHECKING:
    from studio_api.v0_0_1.generated.StudioAPISyncStub import StudioAPISyncStub  # type: ignore

    StudioAPISync: Type[StudioAPISyncStub]

StudioAPISync = create_sync_class(StudioAPIAdapter_v0_0_1)

__all__ = [
    "StudioAPI",
    "StudioAPISync",
    "Input",
    "InputImpl",
    "Types",
]
