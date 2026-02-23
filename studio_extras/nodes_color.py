from typing_extensions import override
from studio_api.latest import StudioExtension, io


class ColorToRGBInt(io.StudioNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="ColorToRGBInt",
            display_name="Color to RGB Int",
            category="utils",
            description="Convert a color to a RGB integer value.",
            inputs=[
                io.Color.Input("color"),
            ],
            outputs=[
                io.Int.Output(display_name="rgb_int"),
            ],
        )

    @classmethod
    def execute(
        cls,
        color: str,
    ) -> io.NodeOutput:
        # expect format #RRGGBB
        if len(color) != 7 or color[0] != "#":
            raise ValueError("Color must be in format #RRGGBB")
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
        return io.NodeOutput(r * 256 * 256 + g * 256 + b)


class ColorExtension(StudioExtension):
    @override
    async def get_node_list(self) -> list[type[io.StudioNode]]:
        return [ColorToRGBInt]


async def studio_entrypoint() -> ColorExtension:
    return ColorExtension()
