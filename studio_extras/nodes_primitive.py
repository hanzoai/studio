import sys
from typing_extensions import override

from studio_api.latest import StudioExtension, io


class String(io.StudioNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="PrimitiveString",
            display_name="String",
            category="utils/primitive",
            inputs=[
                io.String.Input("value"),
            ],
            outputs=[io.String.Output()],
        )

    @classmethod
    def execute(cls, value: str) -> io.NodeOutput:
        return io.NodeOutput(value)


class StringMultiline(io.StudioNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="PrimitiveStringMultiline",
            display_name="String (Multiline)",
            category="utils/primitive",
            essentials_category="Basics",
            inputs=[
                io.String.Input("value", multiline=True),
            ],
            outputs=[io.String.Output()],
        )

    @classmethod
    def execute(cls, value: str) -> io.NodeOutput:
        return io.NodeOutput(value)


class Int(io.StudioNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="PrimitiveInt",
            display_name="Int",
            category="utils/primitive",
            inputs=[
                io.Int.Input("value", min=-sys.maxsize, max=sys.maxsize, control_after_generate=True),
            ],
            outputs=[io.Int.Output()],
        )

    @classmethod
    def execute(cls, value: int) -> io.NodeOutput:
        return io.NodeOutput(value)


class Float(io.StudioNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="PrimitiveFloat",
            display_name="Float",
            category="utils/primitive",
            inputs=[
                io.Float.Input("value", min=-sys.maxsize, max=sys.maxsize, step=0.1),
            ],
            outputs=[io.Float.Output()],
        )

    @classmethod
    def execute(cls, value: float) -> io.NodeOutput:
        return io.NodeOutput(value)


class Boolean(io.StudioNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="PrimitiveBoolean",
            display_name="Boolean",
            category="utils/primitive",
            inputs=[
                io.Boolean.Input("value"),
            ],
            outputs=[io.Boolean.Output()],
        )

    @classmethod
    def execute(cls, value: bool) -> io.NodeOutput:
        return io.NodeOutput(value)


class PrimitivesExtension(StudioExtension):
    @override
    async def get_node_list(self) -> list[type[io.StudioNode]]:
        return [
            String,
            StringMultiline,
            Int,
            Float,
            Boolean,
        ]

async def studio_entrypoint() -> PrimitivesExtension:
    return PrimitivesExtension()
