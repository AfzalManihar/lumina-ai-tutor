"""Allowlisted, schema-validated tools; no generated code execution."""
import ast
import asyncio
import operator
from typing import Literal

from pydantic import BaseModel, Field


class CalculatorArgs(BaseModel):
    expression: str = Field(min_length=1, max_length=120)


class SearchMaterialsArgs(BaseModel):
    query: str = Field(min_length=1, max_length=3000)


class ToolContext(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    role: str
    grade: int | None = None


class SourceCitation(BaseModel):
    filename: str = Field(min_length=1, max_length=260)
    page: int = Field(ge=1)


class EvidenceChunk(BaseModel):
    source_id: str = Field(max_length=160)
    filename: str = Field(max_length=260)
    page: int = Field(ge=1)
    text: str = Field(max_length=4000)


class ToolResult(BaseModel):
    tool: str
    status: Literal["success", "no_results"]
    result: float | None = None
    context: str = Field(default="", max_length=12000)
    sources: list[str] = Field(default_factory=list, max_length=10)
    citations: list[SourceCitation] = Field(default_factory=list, max_length=10)
    evidence: list[EvidenceChunk] = Field(default_factory=list, max_length=10)
    retrieval_count: int = Field(default=0, ge=0, le=10)


class ToolSpec(BaseModel):
    name: str
    description: str
    permission: str
    timeout_seconds: float = Field(gt=0, le=15)
    read_only: bool = True
    args_schema: type[BaseModel]
    return_schema: type[BaseModel]


_ALLOWED = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _calculate(expression: str) -> float:
    if len(expression) > 120:
        raise ValueError("Expression too long")

    tree = ast.parse(expression, mode="eval")

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)

        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value

        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED:
            left, right = visit(node.left), visit(node.right)

            if isinstance(node.op, ast.Pow) and abs(right) > 8:
                raise ValueError("Exponent limit exceeded")

            result = _ALLOWED[type(node.op)](left, right)

            if abs(result) > 1e15:
                raise ValueError("Result limit exceeded")

            return result

        if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED:
            return _ALLOWED[type(node.op)](visit(node.operand))

        raise ValueError("Only basic arithmetic is allowed")

    return visit(tree)


REGISTRY = {
    "calculate": ToolSpec(
        name="calculate",
        description="Evaluate bounded basic arithmetic",
        permission="student",
        timeout_seconds=2.0,
        args_schema=CalculatorArgs,
        return_schema=ToolResult,
    ),
    "search_learning_materials": ToolSpec(
        name="search_learning_materials",
        description="Retrieve authorized study material for the authenticated learner",
        permission="student",
        timeout_seconds=15.0,
        args_schema=SearchMaterialsArgs,
        return_schema=ToolResult,
    ),
}


def list_tools(role: str) -> list[dict]:
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "read_only": tool.read_only,
            "permission": tool.permission,
            "timeout_seconds": tool.timeout_seconds,
            "arguments": tool.args_schema.model_json_schema(),
            "returns": tool.return_schema.model_json_schema(),
        }
        for tool in REGISTRY.values()
        if role == "Student" and tool.permission == "student"
    ]


async def execute_tool(name: str, arguments: dict, context: ToolContext) -> dict:
    spec = REGISTRY.get(name)

    if spec is None:
        raise PermissionError("Tool is not allowlisted")

    if spec.permission == "student" and context.role != "Student":
        raise PermissionError("Not authorized for this tool")

    parsed = spec.args_schema.model_validate(arguments)

    if name == "calculate":
        value = await asyncio.wait_for(
            asyncio.to_thread(_calculate, parsed.expression),
            timeout=spec.timeout_seconds,
        )
        return spec.return_schema.model_validate(
            {
                "tool": name,
                "status": "success",
                "result": value,
            }
        ).model_dump()

    if name == "search_learning_materials":
        if context.grade is None:
            raise PermissionError(
                "A grade is required to search learning materials"
            )

        from chat.chat_query import retrieve_materials

        result = await asyncio.wait_for(
            retrieve_materials(
                parsed.query,
                context.role,
                context.grade,
                context.user_id,
            ),
            timeout=spec.timeout_seconds,
        )

        output = {
            "tool": name,
            "status": (
                "success" if result["retrieval_count"] else "no_results"
            ),
            **result,
        }

        return spec.return_schema.model_validate(output).model_dump()

    raise RuntimeError("Tool has no registered executor")
