"""Multi-language AST and code extractor for agent-brain.

Supports:
- Python & PySpark (.py, .spark)
- Rust (.rs)
- JavaScript (.js, .jsx, .mjs, .cjs)
- TypeScript (.ts, .tsx, .mts, .cts)
- Go (.go)
- Scala (.scala)
- SQL (.sql)

Extracts Class/Struct/Table definitions, Function/Method/Procedure definitions,
and their call/dependency relationships into KùzuDB (:CodeFile, :Class, :Function, :CONTAINS, :CALLS)
with concurrent multi-threaded parallel processing across CPU cores.
"""

from __future__ import annotations

import ast
import concurrent.futures
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from core.database import Database, DatabaseQueryError, get_db

logger = logging.getLogger("agent_brain.ast_parser")

DEFAULT_EXCLUDED_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".agent-memory",
    "node_modules",
    "build",
    "dist",
    ".eggs",
    ".idea",
    ".vscode",
    "target",  # Rust cargo build
    ".bloop",   # Scala build
    ".metals",  # Scala metals
}

SUPPORTED_EXTENSIONS = {
    ".py": "python",
    ".spark": "pyspark",
    ".rs": "rust",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".go": "go",
    ".scala": "scala",
    ".sql": "sql",
}

DISALLOWED_GLOBAL_CALLEES = {
    "__init__",
    "__str__",
    "__repr__",
    "__eq__",
    "print",
    "len",
    "range",
    "str",
    "int",
    "float",
    "bool",
    "dict",
    "list",
    "set",
    "tuple",
    "isinstance",
    "issubclass",
    "getattr",
    "setattr",
    "hasattr",
    "open",
    "super",
    "type",
    "format",
    "get",
    "items",
    "keys",
    "values",
    "append",
    "extend",
    "pop",
    "update",
    "close",
    "split",
    "strip",
    "join",
    "replace",
    "lower",
    "upper",
    "startswith",
    "endswith",
    "write",
    "read",
    "encode",
    "decode",
}


@dataclass
class CallInfo:
    """Represents a call or reference relationship."""

    caller_id: str
    callee_name: str
    line_number: int
    callee_id: Optional[str] = None
    callee_path: Optional[str] = None


@dataclass
class FunctionInfo:
    """Represents a function, method, or procedure definition."""

    id: str
    name: str
    qualified_name: str
    path: str
    is_method: bool
    is_async: bool
    parent_type: str  # 'file', 'class', or 'function'
    parent_id: str
    line_number: int
    end_line_number: int
    docstring: str = ""


@dataclass
class ClassInfo:
    """Represents a class, struct, trait, or table definition."""

    id: str
    name: str
    qualified_name: str
    path: str
    parent_type: str  # 'file' or 'class'
    parent_id: str
    line_number: int
    end_line_number: int
    docstring: str = ""


@dataclass
class FileParseResult:
    """Parse output for a single code file."""

    path: str
    filename: str
    language: str = "unknown"
    classes: List[ClassInfo] = field(default_factory=list)
    functions: List[FunctionInfo] = field(default_factory=list)
    calls: List[CallInfo] = field(default_factory=list)
    has_syntax_error: bool = False
    error_message: Optional[str] = None


class PythonCodeVisitor(ast.NodeVisitor):
    """Python AST visitor extracting classes, functions, and call relationships."""

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self.scope_stack: List[Dict[str, str]] = []  # dict with 'type', 'name', 'id'
        self.classes: List[ClassInfo] = []
        self.functions: List[FunctionInfo] = []
        self.calls: List[CallInfo] = []
        self.is_pyspark: bool = False
        self.imports: Dict[str, str] = {}  # local_alias -> module or fully qualified symbol
        self.local_definitions: Set[str] = set()

    def _get_parent_info(self) -> tuple[str, str]:
        """Return (parent_type, parent_id) based on current scope."""
        if not self.scope_stack:
            return ("file", self.file_path)
        top = self.scope_stack[-1]
        return (top["type"], top["id"])

    def _build_qualified_name(self, current_name: str) -> str:
        """Construct scoped qualified name (e.g. OuterClass.InnerClass.method)."""
        scoped = [scope["name"] for scope in self.scope_stack] + [current_name]
        return ".".join(scoped)

    @staticmethod
    def _extract_cleaned_docstring(node: ast.AST) -> str:
        """Extract and compact docstrings, removing redundant whitespace lines."""
        raw = ast.get_docstring(node, clean=True)
        if not raw:
            return ""
        lines = [line.strip() for line in raw.strip().splitlines()]
        compacted: List[str] = []
        prev_blank = False
        for line in lines:
            if not line:
                if not prev_blank:
                    compacted.append("")
                    prev_blank = True
            else:
                compacted.append(line)
                prev_blank = False
        return "\n".join(compacted)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local_name = alias.asname or alias.name
            self.imports[local_name] = alias.name
            if "pyspark" in alias.name:
                self.is_pyspark = True
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        for alias in node.names:
            local_name = alias.asname or alias.name
            target = f"{mod}.{alias.name}" if mod else alias.name
            self.imports[local_name] = target
        if mod and "pyspark" in mod:
            self.is_pyspark = True
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        class_name = node.name
        self.local_definitions.add(class_name)
        qualified_name = self._build_qualified_name(class_name)
        class_id = f"{self.file_path}:{qualified_name}"
        parent_type, parent_id = self._get_parent_info()

        docstring = self._extract_cleaned_docstring(node)
        end_lineno = getattr(node, "end_lineno", node.lineno)

        self.classes.append(
            ClassInfo(
                id=class_id,
                name=class_name,
                qualified_name=qualified_name,
                path=self.file_path,
                parent_type=parent_type,
                parent_id=parent_id,
                line_number=node.lineno,
                end_line_number=end_lineno,
                docstring=docstring,
            )
        )

        self.scope_stack.append({"type": "class", "name": class_name, "id": class_id})
        self.generic_visit(node)
        self.scope_stack.pop()

    def _handle_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, is_async: bool
    ) -> None:
        func_name = node.name
        self.local_definitions.add(func_name)
        qualified_name = self._build_qualified_name(func_name)
        func_id = f"{self.file_path}:{qualified_name}"
        parent_type, parent_id = self._get_parent_info()
        is_method = any(s["type"] == "class" for s in self.scope_stack)

        docstring = self._extract_cleaned_docstring(node)
        end_lineno = getattr(node, "end_lineno", node.lineno)

        self.functions.append(
            FunctionInfo(
                id=func_id,
                name=func_name,
                qualified_name=qualified_name,
                path=self.file_path,
                is_method=is_method,
                is_async=is_async,
                parent_type=parent_type,
                parent_id=parent_id,
                line_number=node.lineno,
                end_line_number=end_lineno,
                docstring=docstring,
            )
        )

        self.scope_stack.append({"type": "function", "name": func_name, "id": func_id})
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._handle_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._handle_function(node, is_async=True)

    def visit_Call(self, node: ast.Call) -> None:
        current_func = None
        for scope in reversed(self.scope_stack):
            if scope["type"] == "function":
                current_func = scope
                break

        if current_func:
            callee_name, callee_id, callee_path = self._resolve_call_target(node.func)
            if callee_name:
                self.calls.append(
                    CallInfo(
                        caller_id=current_func["id"],
                        callee_name=callee_name,
                        line_number=node.lineno,
                        callee_id=callee_id,
                        callee_path=callee_path,
                    )
                )

        self.generic_visit(node)

    def _resolve_call_target(
        self, func_node: ast.AST
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Resolve callee name, optional qualified callee_id, and optional callee_path."""
        if isinstance(func_node, ast.Name):
            name = func_node.id
            callee_id = None
            callee_path = None
            if name in self.local_definitions:
                callee_id = f"{self.file_path}:{name}"
                callee_path = self.file_path
            elif name in self.imports:
                callee_path = self.imports[name]
            return name, callee_id, callee_path

        if isinstance(func_node, ast.Attribute):
            name = func_node.attr
            callee_id = None
            callee_path = None
            if isinstance(func_node.value, ast.Name):
                recv = func_node.value.id
                if recv in {"self", "cls"}:
                    for scope in reversed(self.scope_stack):
                        if scope["type"] == "class":
                            callee_id = f"{self.file_path}:{scope['name']}.{name}"
                            callee_path = self.file_path
                            break
                elif recv in self.imports:
                    callee_path = f"{self.imports[recv]}.{name}"
            return name, callee_id, callee_path

        return None, None, None

    @staticmethod
    def _resolve_callee_name(func_node: ast.AST) -> Optional[str]:
        if isinstance(func_node, ast.Name):
            return func_node.id
        if isinstance(func_node, ast.Attribute):
            return func_node.attr
        return None


# Backward compatibility alias
CodeVisitor = PythonCodeVisitor


class TreeSitterParser:
    """Multi-language AST parser for non-Python languages using Tree-sitter grammars."""

    _cached_languages = {}

    @classmethod
    def get_language(cls, lang: str):
        """Load and cache tree-sitter Language instance for the target grammar."""
        if lang in cls._cached_languages:
            return cls._cached_languages[lang]

        import importlib
        import tree_sitter

        try:
            if lang == "rust":
                mod = importlib.import_module("tree_sitter_rust")
                l = tree_sitter.Language(mod.language())
            elif lang == "javascript":
                mod = importlib.import_module("tree_sitter_javascript")
                l = tree_sitter.Language(mod.language())
            elif lang == "typescript":
                mod = importlib.import_module("tree_sitter_typescript")
                l = tree_sitter.Language(mod.language_typescript())
            elif lang == "go":
                mod = importlib.import_module("tree_sitter_go")
                l = tree_sitter.Language(mod.language())
            elif lang == "scala":
                mod = importlib.import_module("tree_sitter_scala")
                l = tree_sitter.Language(mod.language())
            elif lang == "sql":
                mod = importlib.import_module("tree_sitter_sql")
                l = tree_sitter.Language(mod.language())
            else:
                return None
            cls._cached_languages[lang] = l
            return l
        except Exception as err:
            logger.warning(f"Could not load tree-sitter grammar for '{lang}': {err}")
            return None

    @classmethod
    def parse_rust(cls, source: str, file_path: str, filename: str) -> FileParseResult:
        """Parse Rust source (.rs) using Tree-sitter AST."""
        res = FileParseResult(path=file_path, filename=filename, language="rust")
        lang = cls.get_language("rust")
        if not lang:
            return res
        import tree_sitter
        parser = tree_sitter.Parser(lang)
        src_bytes = source.encode("utf-8")
        tree = parser.parse(src_bytes)

        current_impl = None
        current_fn_id = None

        def walk(node):
            nonlocal current_impl, current_fn_id
            ntype = node.type
            if ntype in {"struct_item", "enum_item", "trait_item"}:
                name_node = node.child_by_field_name("name")
                if name_node:
                    name = src_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8")
                    res.classes.append(
                        ClassInfo(
                            id=f"{file_path}:{name}",
                            name=name,
                            qualified_name=name,
                            path=file_path,
                            parent_type="file",
                            parent_id=file_path,
                            line_number=node.start_point[0] + 1,
                            end_line_number=node.end_point[0] + 1,
                        )
                    )
            elif ntype == "impl_item":
                type_node = node.child_by_field_name("type")
                if type_node:
                    prev_impl = current_impl
                    current_impl = src_bytes[type_node.start_byte:type_node.end_byte].decode("utf-8")
                    body = node.child_by_field_name("body")
                    if body:
                        for c in body.children:
                            walk(c)
                    current_impl = prev_impl
                    return
            elif ntype == "function_item":
                name_node = node.child_by_field_name("name")
                if name_node:
                    name = src_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8")
                    is_method = current_impl is not None
                    qname = f"{current_impl}.{name}" if is_method else name
                    fid = f"{file_path}:{qname}"
                    prev_fn = current_fn_id
                    current_fn_id = fid
                    res.functions.append(
                        FunctionInfo(
                            id=fid,
                            name=name,
                            qualified_name=qname,
                            path=file_path,
                            is_method=is_method,
                            is_async=any(c.type == "async" for c in node.children),
                            parent_type="class" if is_method else "file",
                            parent_id=f"{file_path}:{current_impl}" if is_method else file_path,
                            line_number=node.start_point[0] + 1,
                            end_line_number=node.end_point[0] + 1,
                        )
                    )
                    body = node.child_by_field_name("body")
                    if body:
                        for c in body.children:
                            walk(c)
                    current_fn_id = prev_fn
                    return
            elif ntype == "call_expression" and current_fn_id:
                fn_node = node.child_by_field_name("function")
                if fn_node:
                    callee_text = src_bytes[fn_node.start_byte:fn_node.end_byte].decode("utf-8")
                    callee_name = callee_text.split(".")[-1].split("::")[-1].strip()
                    if callee_name and callee_name not in DISALLOWED_GLOBAL_CALLEES:
                        res.calls.append(
                            CallInfo(
                                caller_id=current_fn_id,
                                callee_name=callee_name,
                                line_number=node.start_point[0] + 1,
                            )
                        )
            for c in node.children:
                walk(c)

        walk(tree.root_node)
        return res

    @classmethod
    def parse_js_ts(cls, source: str, file_path: str, filename: str, language: str) -> FileParseResult:
        """Parse JavaScript and TypeScript (.js, .jsx, .ts, .tsx) using Tree-sitter AST."""
        res = FileParseResult(path=file_path, filename=filename, language=language)
        grammar_key = "typescript" if "typescript" in language or filename.endswith((".ts", ".tsx")) else "javascript"
        lang = cls.get_language(grammar_key)
        if not lang:
            return res
        import tree_sitter
        parser = tree_sitter.Parser(lang)
        src_bytes = source.encode("utf-8")
        tree = parser.parse(src_bytes)

        current_class = None
        current_fn_id = None

        def walk(node):
            nonlocal current_class, current_fn_id
            ntype = node.type
            if ntype in {"class_declaration", "interface_declaration"}:
                name_node = node.child_by_field_name("name")
                if name_node:
                    name = src_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8")
                    res.classes.append(
                        ClassInfo(
                            id=f"{file_path}:{name}",
                            name=name,
                            qualified_name=name,
                            path=file_path,
                            parent_type="file",
                            parent_id=file_path,
                            line_number=node.start_point[0] + 1,
                            end_line_number=node.end_point[0] + 1,
                        )
                    )
                    prev_cls = current_class
                    current_class = name
                    body = node.child_by_field_name("body")
                    if body:
                        for c in body.children:
                            walk(c)
                    current_class = prev_cls
                    return
            elif ntype in {"method_definition", "function_declaration"}:
                name_node = node.child_by_field_name("name")
                if name_node:
                    name = src_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8")
                    is_method = current_class is not None
                    qname = f"{current_class}.{name}" if is_method else name
                    fid = f"{file_path}:{qname}"
                    prev_fn = current_fn_id
                    current_fn_id = fid
                    is_async = any(c.type == "async" for c in node.children)
                    res.functions.append(
                        FunctionInfo(
                            id=fid,
                            name=name,
                            qualified_name=qname,
                            path=file_path,
                            is_method=is_method,
                            is_async=is_async,
                            parent_type="class" if is_method else "file",
                            parent_id=f"{file_path}:{current_class}" if is_method else file_path,
                            line_number=node.start_point[0] + 1,
                            end_line_number=node.end_point[0] + 1,
                        )
                    )
                    body = node.child_by_field_name("body")
                    if body:
                        for c in body.children:
                            walk(c)
                    current_fn_id = prev_fn
                    return
            elif ntype == "variable_declarator":
                val = node.child_by_field_name("value")
                name_node = node.child_by_field_name("name")
                if val and val.type in {"arrow_function", "function_expression"} and name_node:
                    name = src_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8")
                    fid = f"{file_path}:{name}"
                    prev_fn = current_fn_id
                    current_fn_id = fid
                    is_async = any(c.type == "async" for c in val.children)
                    res.functions.append(
                        FunctionInfo(
                            id=fid,
                            name=name,
                            qualified_name=name,
                            path=file_path,
                            is_method=False,
                            is_async=is_async,
                            parent_type="file",
                            parent_id=file_path,
                            line_number=node.start_point[0] + 1,
                            end_line_number=node.end_point[0] + 1,
                        )
                    )
                    body = val.child_by_field_name("body")
                    if body:
                        walk(body)
                    current_fn_id = prev_fn
                    return
            elif ntype == "call_expression" and current_fn_id:
                fn_node = node.child_by_field_name("function")
                if fn_node:
                    callee_text = src_bytes[fn_node.start_byte:fn_node.end_byte].decode("utf-8")
                    callee_name = callee_text.split(".")[-1].strip()
                    if callee_name and callee_name not in DISALLOWED_GLOBAL_CALLEES:
                        res.calls.append(
                            CallInfo(
                                caller_id=current_fn_id,
                                callee_name=callee_name,
                                line_number=node.start_point[0] + 1,
                            )
                        )
            for c in node.children:
                walk(c)

        walk(tree.root_node)
        return res

    @classmethod
    def parse_go(cls, source: str, file_path: str, filename: str) -> FileParseResult:
        """Parse Go source (.go) using Tree-sitter AST."""
        res = FileParseResult(path=file_path, filename=filename, language="go")
        lang = cls.get_language("go")
        if not lang:
            return res
        import tree_sitter
        parser = tree_sitter.Parser(lang)
        src_bytes = source.encode("utf-8")
        tree = parser.parse(src_bytes)

        current_fn_id = None

        def extract_type_name(node):
            if node.type == "type_identifier":
                return src_bytes[node.start_byte:node.end_byte].decode("utf-8")
            for c in node.children:
                t = extract_type_name(c)
                if t:
                    return t
            return None

        def walk(node):
            nonlocal current_fn_id
            ntype = node.type
            if ntype == "type_spec":
                name_node = node.child_by_field_name("name")
                if name_node:
                    name = src_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8")
                    res.classes.append(
                        ClassInfo(
                            id=f"{file_path}:{name}",
                            name=name,
                            qualified_name=name,
                            path=file_path,
                            parent_type="file",
                            parent_id=file_path,
                            line_number=node.start_point[0] + 1,
                            end_line_number=node.end_point[0] + 1,
                        )
                    )
            elif ntype == "method_declaration":
                name_node = node.child_by_field_name("name")
                recv_node = node.child_by_field_name("receiver")
                if name_node and recv_node:
                    name = src_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8")
                    recv_type = extract_type_name(recv_node) or "unknown"
                    qname = f"{recv_type}.{name}"
                    fid = f"{file_path}:{qname}"
                    prev_fn = current_fn_id
                    current_fn_id = fid
                    res.functions.append(
                        FunctionInfo(
                            id=fid,
                            name=name,
                            qualified_name=qname,
                            path=file_path,
                            is_method=True,
                            is_async=False,
                            parent_type="class",
                            parent_id=f"{file_path}:{recv_type}",
                            line_number=node.start_point[0] + 1,
                            end_line_number=node.end_point[0] + 1,
                        )
                    )
                    body = node.child_by_field_name("body")
                    if body:
                        for c in body.children:
                            walk(c)
                    current_fn_id = prev_fn
                    return
            elif ntype == "function_declaration":
                name_node = node.child_by_field_name("name")
                if name_node:
                    name = src_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8")
                    fid = f"{file_path}:{name}"
                    prev_fn = current_fn_id
                    current_fn_id = fid
                    res.functions.append(
                        FunctionInfo(
                            id=fid,
                            name=name,
                            qualified_name=name,
                            path=file_path,
                            is_method=False,
                            is_async=False,
                            parent_type="file",
                            parent_id=file_path,
                            line_number=node.start_point[0] + 1,
                            end_line_number=node.end_point[0] + 1,
                        )
                    )
                    body = node.child_by_field_name("body")
                    if body:
                        for c in body.children:
                            walk(c)
                    current_fn_id = prev_fn
                    return
            elif ntype == "call_expression" and current_fn_id:
                fn_node = node.child_by_field_name("function")
                if fn_node:
                    callee_text = src_bytes[fn_node.start_byte:fn_node.end_byte].decode("utf-8")
                    callee_name = callee_text.split(".")[-1].strip()
                    if callee_name and callee_name not in DISALLOWED_GLOBAL_CALLEES:
                        res.calls.append(
                            CallInfo(
                                caller_id=current_fn_id,
                                callee_name=callee_name,
                                line_number=node.start_point[0] + 1,
                            )
                        )
            for c in node.children:
                walk(c)

        walk(tree.root_node)
        return res

    @classmethod
    def parse_scala(cls, source: str, file_path: str, filename: str) -> FileParseResult:
        """Parse Scala source (.scala) using Tree-sitter AST."""
        res = FileParseResult(path=file_path, filename=filename, language="scala")
        lang = cls.get_language("scala")
        if not lang:
            return res
        import tree_sitter
        parser = tree_sitter.Parser(lang)
        src_bytes = source.encode("utf-8")
        tree = parser.parse(src_bytes)

        current_class = None
        current_fn_id = None

        def walk(node):
            nonlocal current_class, current_fn_id
            ntype = node.type
            if ntype in {"class_definition", "object_definition", "trait_definition"}:
                name_node = node.child_by_field_name("name")
                if name_node:
                    name = src_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8")
                    res.classes.append(
                        ClassInfo(
                            id=f"{file_path}:{name}",
                            name=name,
                            qualified_name=name,
                            path=file_path,
                            parent_type="file",
                            parent_id=file_path,
                            line_number=node.start_point[0] + 1,
                            end_line_number=node.end_point[0] + 1,
                        )
                    )
                    prev_cls = current_class
                    current_class = name
                    body = node.child_by_field_name("body")
                    if body:
                        for c in body.children:
                            walk(c)
                    current_class = prev_cls
                    return
            elif ntype == "function_definition":
                name_node = node.child_by_field_name("name")
                if name_node:
                    name = src_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8")
                    is_method = current_class is not None
                    qname = f"{current_class}.{name}" if is_method else name
                    fid = f"{file_path}:{qname}"
                    prev_fn = current_fn_id
                    current_fn_id = fid
                    res.functions.append(
                        FunctionInfo(
                            id=fid,
                            name=name,
                            qualified_name=qname,
                            path=file_path,
                            is_method=is_method,
                            is_async=False,
                            parent_type="class" if is_method else "file",
                            parent_id=f"{file_path}:{current_class}" if is_method else file_path,
                            line_number=node.start_point[0] + 1,
                            end_line_number=node.end_point[0] + 1,
                        )
                    )
                    body = node.child_by_field_name("body")
                    if body:
                        for c in body.children:
                            walk(c)
                    current_fn_id = prev_fn
                    return
            elif ntype == "call_expression" and current_fn_id:
                fn_node = node.child_by_field_name("function")
                if fn_node:
                    callee_text = src_bytes[fn_node.start_byte:fn_node.end_byte].decode("utf-8")
                    callee_name = callee_text.split(".")[-1].strip()
                    if callee_name and callee_name not in DISALLOWED_GLOBAL_CALLEES:
                        res.calls.append(
                            CallInfo(
                                caller_id=current_fn_id,
                                callee_name=callee_name,
                                line_number=node.start_point[0] + 1,
                            )
                        )
            for c in node.children:
                walk(c)

        walk(tree.root_node)
        return res

    @classmethod
    def parse_sql(cls, source: str, file_path: str, filename: str) -> FileParseResult:
        """Parse SQL source (.sql) using Tree-sitter AST."""
        res = FileParseResult(path=file_path, filename=filename, language="sql")
        lang = cls.get_language("sql")
        if not lang:
            return res
        import tree_sitter
        parser = tree_sitter.Parser(lang)
        src_bytes = source.encode("utf-8")
        tree = parser.parse(src_bytes)

        def walk(node):
            ntype = node.type
            if ntype in {"create_table", "create_view"}:
                for c in node.children:
                    if c.type in {"object_reference", "identifier"}:
                        name = src_bytes[c.start_byte:c.end_byte].decode("utf-8")
                        res.classes.append(
                            ClassInfo(
                                id=f"{file_path}:{name}",
                                name=name,
                                qualified_name=name,
                                path=file_path,
                                parent_type="file",
                                parent_id=file_path,
                                line_number=node.start_point[0] + 1,
                                end_line_number=node.end_point[0] + 1,
                            )
                        )
                        break
            elif ntype in {"create_function", "create_procedure"}:
                for c in node.children:
                    if c.type in {"object_reference", "identifier"}:
                        name = src_bytes[c.start_byte:c.end_byte].decode("utf-8")
                        res.functions.append(
                            FunctionInfo(
                                id=f"{file_path}:{name}",
                                name=name,
                                qualified_name=name,
                                path=file_path,
                                is_method=False,
                                is_async=False,
                                parent_type="file",
                                parent_id=file_path,
                                line_number=node.start_point[0] + 1,
                                end_line_number=node.end_point[0] + 1,
                            )
                        )
                        break
            for c in node.children:
                walk(c)

        walk(tree.root_node)
        return res


# Backward compatibility alias
MultiLanguageParser = TreeSitterParser



class ASTParser:
    """Multi-language parser and graph loader for codebase analysis."""

    def __init__(self, excluded_dirs: Optional[Set[str]] = None) -> None:
        self.excluded_dirs = (
            set(excluded_dirs) if excluded_dirs is not None else set(DEFAULT_EXCLUDED_DIRS)
        )

    def parse_source(
        self,
        source: str,
        file_path: str = "<string>",
        filename: str = "",
        language: Optional[str] = None,
    ) -> FileParseResult:
        """Parse source code string into a FileParseResult, gracefully handling errors."""
        normalized_path = file_path.replace("\\", "/")
        if not filename:
            filename = Path(file_path).name or "unknown"

        ext = Path(filename).suffix.lower()
        lang = language or SUPPORTED_EXTENSIONS.get(ext, "unknown")

        # 1. Non-Python parsers using Tree-sitter AST
        if lang == "rust":
            return TreeSitterParser.parse_rust(source, normalized_path, filename)
        if lang in {"javascript", "typescript"}:
            return TreeSitterParser.parse_js_ts(source, normalized_path, filename, lang)
        if lang == "go":
            return TreeSitterParser.parse_go(source, normalized_path, filename)
        if lang == "scala":
            return TreeSitterParser.parse_scala(source, normalized_path, filename)
        if lang == "sql":
            return TreeSitterParser.parse_sql(source, normalized_path, filename)

        # 2. Python / PySpark parser

        result = FileParseResult(path=normalized_path, filename=filename, language=lang or "python")
        try:
            tree = ast.parse(source, filename=file_path)
            visitor = PythonCodeVisitor(normalized_path)
            visitor.visit(tree)

            result.classes = visitor.classes
            result.functions = visitor.functions
            result.calls = visitor.calls
            if visitor.is_pyspark or ext == ".spark":
                result.language = "pyspark"
            else:
                result.language = "python"

        except SyntaxError as err:
            err_msg = f"Syntax error at line {err.lineno}, col {err.offset}: {err.msg}"
            logger.warning(f"Syntax error in '{file_path}': {err_msg}")
            result.has_syntax_error = True
            result.error_message = err_msg

        except Exception as err:
            err_msg = f"Unexpected AST parse error: {err}"
            logger.warning(f"Error parsing '{file_path}': {err_msg}")
            result.has_syntax_error = True
            result.error_message = err_msg

        return result

    def parse_file(
        self, file_path: Path | str, root_dir: Optional[Path | str] = None
    ) -> FileParseResult:
        """Parse a single code file from disk."""
        target = Path(file_path).resolve()

        if root_dir:
            try:
                rel_path = target.relative_to(Path(root_dir).resolve())
                display_path = str(rel_path).replace("\\", "/")
            except ValueError:
                display_path = str(target).replace("\\", "/")
        else:
            display_path = str(target).replace("\\", "/")

        filename = target.name
        ext = target.suffix.lower()
        language = SUPPORTED_EXTENSIONS.get(ext, "unknown")

        try:
            source = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                source = target.read_text(encoding="latin-1")
            except Exception as err:
                logger.warning(f"Encoding error reading {target}: {err}")
                return FileParseResult(
                    path=display_path,
                    filename=filename,
                    language=language,
                    has_syntax_error=True,
                    error_message=f"Encoding error: {err}",
                )
        except Exception as err:
            logger.warning(f"Failed to read file {target}: {err}")
            return FileParseResult(
                path=display_path,
                filename=filename,
                language=language,
                has_syntax_error=True,
                error_message=f"File read error: {err}",
            )

        return self.parse_source(
            source, file_path=display_path, filename=filename, language=language
        )

    def scan_directory(
        self,
        target_dir: Path | str,
        max_workers: Optional[int] = None,
    ) -> List[FileParseResult]:
        """Scan codebase directory in parallel across CPU cores for all supported languages."""
        root = Path(target_dir).resolve()
        results: List[FileParseResult] = []

        if not root.exists():
            logger.error(f"Target directory '{root}' does not exist.")
            return results

        logger.info(f"Scanning codebase directory: {root}")

        # Step 1: Collect all eligible file paths matching supported extensions
        files_to_parse: List[Path] = []
        for current_root, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in self.excluded_dirs]
            for file in files:
                ext = Path(file).suffix.lower()
                if ext in SUPPORTED_EXTENSIONS:
                    files_to_parse.append(Path(current_root) / file)

        total_files = len(files_to_parse)
        if total_files == 0:
            logger.info("No matching supported files found.")
            return results

        # Step 2: Multi-threaded parallel file processing
        workers = max_workers or min(32, (os.cpu_count() or 4) + 4)
        logger.info(f"Parsing {total_files} files in parallel using {workers} workers...")

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_file = {
                executor.submit(self.parse_file, full_path, root): full_path
                for full_path in files_to_parse
            }
            for future in concurrent.futures.as_completed(future_to_file):
                try:
                    res = future.result()
                    results.append(res)
                except Exception as err:
                    failed_file = future_to_file[future]
                    logger.warning(f"Worker failed on {failed_file}: {err}")

        logger.info(
            f"Scanned {len(results)} files "
            f"({sum(1 for r in results if r.has_syntax_error)} with syntax/read errors)."
        )
        return results

    def save_to_graph(
        self, parse_results: List[FileParseResult], db: Optional[Database] = None
    ) -> Dict[str, int]:
        """Write extracted AST nodes and edges into KùzuDB graph."""
        database = db or get_db()

        stats = {
            "files": 0,
            "classes": 0,
            "functions": 0,
            "contains_edges": 0,
            "calls_edges": 0,
            "syntax_errors": 0,
        }

        # Step 1: Ensure indexes for query speed
        self._ensure_indexes(database)

        # Step 2: Ingest CodeFile, Class, and Function nodes
        for result in parse_results:
            if result.has_syntax_error:
                stats["syntax_errors"] += 1

            # Merge :CodeFile with language tag
            try:
                database.execute_query(
                    """
                    MERGE (f:CodeFile {path: $path})
                    SET f.filename = $filename,
                        f.has_syntax_error = $has_syntax_error,
                        f.language = $language
                    """,
                    {
                        "path": result.path,
                        "filename": result.filename,
                        "has_syntax_error": result.has_syntax_error,
                        "language": result.language,
                    },
                )
                stats["files"] += 1
            except Exception as err:
                logger.error(f"Failed to save CodeFile '{result.path}': {err}")

            # Merge :Class nodes
            for cls in result.classes:
                try:
                    database.execute_query(
                        """
                        MERGE (c:Class {id: $id})
                        SET c.name = $name,
                            c.qualified_name = $qualified_name,
                            c.path = $path,
                            c.docstring = $docstring,
                            c.line_number = $line_number,
                            c.end_line_number = $end_line_number
                        """,
                        {
                            "id": cls.id,
                            "name": cls.name,
                            "qualified_name": cls.qualified_name,
                            "path": cls.path,
                            "docstring": cls.docstring,
                            "line_number": cls.line_number,
                            "end_line_number": cls.end_line_number,
                        },
                    )
                    stats["classes"] += 1
                except Exception as err:
                    logger.error(f"Failed to save Class '{cls.id}': {err}")

            # Merge :Function nodes
            for fn in result.functions:
                try:
                    database.execute_query(
                        """
                        MERGE (fn:Function {id: $id})
                        SET fn.name = $name,
                            fn.qualified_name = $qualified_name,
                            fn.path = $path,
                            fn.is_method = $is_method,
                            fn.is_async = $is_async,
                            fn.docstring = $docstring,
                            fn.line_number = $line_number,
                            fn.end_line_number = $end_line_number
                        """,
                        {
                            "id": fn.id,
                            "name": fn.name,
                            "qualified_name": fn.qualified_name,
                            "path": fn.path,
                            "is_method": fn.is_method,
                            "is_async": fn.is_async,
                            "docstring": fn.docstring,
                            "line_number": fn.line_number,
                            "end_line_number": fn.end_line_number,
                        },
                    )
                    stats["functions"] += 1
                except Exception as err:
                    logger.error(f"Failed to save Function '{fn.id}': {err}")

        # Step 3: Establish :CONTAINS relationships
        for result in parse_results:
            # File -> Class
            for cls in result.classes:
                if cls.parent_type == "file":
                    if self._create_edge(
                        database,
                        """
                        MATCH (f:CodeFile {path: $file_path}), (c:Class {id: $class_id})
                        MERGE (f)-[:CONTAINS]->(c)
                        """,
                        {"file_path": result.path, "class_id": cls.id},
                    ):
                        stats["contains_edges"] += 1
                elif cls.parent_type == "class":
                    if self._create_edge(
                        database,
                        """
                        MATCH (parent:Class {id: $parent_id}), (child:Class {id: $child_id})
                        MERGE (parent)-[:CONTAINS]->(child)
                        """,
                        {"parent_id": cls.parent_id, "child_id": cls.id},
                    ):
                        stats["contains_edges"] += 1

            # File/Class/Function -> Function
            for fn in result.functions:
                if fn.parent_type == "file":
                    if self._create_edge(
                        database,
                        """
                        MATCH (f:CodeFile {path: $file_path}), (fn:Function {id: $func_id})
                        MERGE (f)-[:CONTAINS]->(fn)
                        """,
                        {"file_path": result.path, "func_id": fn.id},
                    ):
                        stats["contains_edges"] += 1
                elif fn.parent_type == "class":
                    if self._create_edge(
                        database,
                        """
                        MATCH (c:Class {id: $class_id}), (fn:Function {id: $func_id})
                        MERGE (c)-[:CONTAINS]->(fn)
                        """,
                        {"class_id": fn.parent_id, "func_id": fn.id},
                    ):
                        stats["contains_edges"] += 1
                elif fn.parent_type == "function":
                    if self._create_edge(
                        database,
                        """
                        MATCH (parent:Function {id: $parent_id}), (child:Function {id: $child_id})
                        MERGE (parent)-[:CONTAINS]->(child)
                        """,
                        {"parent_id": fn.parent_id, "child_id": fn.id},
                    ):
                        stats["contains_edges"] += 1

        # Step 4: Establish :CALLS relationships
        seen_calls: Set[Tuple[str, str]] = set()
        for result in parse_results:
            for call in result.calls:
                # 4a. If specific callee_id was resolved, link directly
                if call.callee_id:
                    call_key = (call.caller_id, call.callee_id)
                    if call_key in seen_calls:
                        continue
                    seen_calls.add(call_key)
                    fn_linked = self._create_edge(
                        database,
                        """
                        MATCH (caller:Function {id: $caller_id}), (callee:Function {id: $callee_id})
                        MERGE (caller)-[:CALLS]->(callee)
                        """,
                        {"caller_id": call.caller_id, "callee_id": call.callee_id},
                    )
                    if fn_linked:
                        stats["calls_edges"] += 1
                    else:
                        cls_linked = self._create_edge(
                            database,
                            """
                            MATCH (caller:Function {id: $caller_id}), (callee:Class {id: $callee_id})
                            MERGE (caller)-[:CALLS]->(callee)
                            """,
                            {"caller_id": call.caller_id, "callee_id": call.callee_id},
                        )
                        if cls_linked:
                            stats["calls_edges"] += 1
                    continue

                call_key = (call.caller_id, call.callee_name)
                if call_key in seen_calls:
                    continue
                seen_calls.add(call_key)

                # Skip linking common generic builtins/unqualified methods across the whole repo
                if call.callee_name in DISALLOWED_GLOBAL_CALLEES:
                    continue

                # 4b. Match against functions in codebase
                fn_linked = self._create_edge(
                    database,
                    """
                    MATCH (caller:Function {id: $caller_id}), (callee:Function {name: $callee_name})
                    MERGE (caller)-[:CALLS]->(callee)
                    """,
                    {"caller_id": call.caller_id, "callee_name": call.callee_name},
                )
                if fn_linked:
                    stats["calls_edges"] += 1

                # 4c. Match against classes/structs/tables
                cls_linked = self._create_edge(
                    database,
                    """
                    MATCH (caller:Function {id: $caller_id}), (callee:Class {name: $callee_name})
                    MERGE (caller)-[:CALLS]->(callee)
                    """,
                    {"caller_id": call.caller_id, "callee_name": call.callee_name},
                )
                if cls_linked:
                    stats["calls_edges"] += 1

        logger.info(f"Graph update complete: {stats}")
        return stats

    def purge_files_from_graph(
        self, file_paths: List[str], db: Optional[Database] = None
    ) -> int:
        """Purge files and their contained classes/functions from the graph.

        Args:
            file_paths: List of relative or absolute file paths to purge.
            db: Optional Database instance.

        Returns:
            Number of purged files.
        """
        database = db or get_db()
        purged = 0
        for path in file_paths:
            normalized_path = str(path).replace("\\", "/")
            try:
                # 1. Detach delete functions contained in this file
                database.execute_query(
                    """
                    MATCH (f:CodeFile {path: $path})
                    OPTIONAL MATCH (f)-[:CONTAINS*1..2]->(fn:Function)
                    DETACH DELETE fn
                    """,
                    {"path": normalized_path},
                )
                # 2. Detach delete classes contained in this file
                database.execute_query(
                    """
                    MATCH (f:CodeFile {path: $path})
                    OPTIONAL MATCH (f)-[:CONTAINS*1..2]->(c:Class)
                    DETACH DELETE c
                    """,
                    {"path": normalized_path},
                )
                # 3. Delete the CodeFile node itself
                database.execute_query(
                    """
                    MATCH (f:CodeFile {path: $path})
                    DETACH DELETE f
                    """,
                    {"path": normalized_path},
                )
                purged += 1
            except Exception as err:
                logger.debug(f"Notice while purging file '{normalized_path}': {err}")
        return purged

    def reconcile_batch_in_graph(
        self,
        deleted_paths: List[str],
        parse_results: List[FileParseResult],
        db: Optional[Database] = None,
    ) -> Dict[str, int]:
        """Atomically reconcile a batch of file changes (purging old AST nodes and inserting new ones).

        Args:
            deleted_paths: List of file paths to remove completely (e.g. deleted files).
            parse_results: List of freshly parsed FileParseResults for created/modified files.
            db: Optional Database instance.

        Returns:
            Stats dictionary detailing reconciled counts.
        """
        database = db or get_db()

        # All affected paths: deleted files + modified files whose old nodes must be purged
        all_to_purge = list(
            set(
                [str(p).replace("\\", "/") for p in deleted_paths]
                + [r.path for r in parse_results]
            )
        )
        self.purge_files_from_graph(all_to_purge, db=database)

        if not parse_results:
            return {
                "purged_files": len(all_to_purge),
                "files": 0,
                "classes": 0,
                "functions": 0,
                "contains_edges": 0,
                "calls_edges": 0,
            }

        # Re-insert the new parse results
        stats = self.save_to_graph(parse_results, db=database)
        stats["purged_files"] = len(all_to_purge)
        return stats

    @staticmethod
    def _create_edge(database: Database, query: str, params: Dict[str, Any]) -> bool:
        """Helper to execute an edge query safely."""
        try:
            database.execute_query(query, params)
            return True
        except Exception as err:
            logger.debug(f"Edge creation skipped/failed: {err}")
            return False

    @staticmethod
    def _ensure_indexes(database: Database) -> None:
        """Ensure indexes for primary keys and lookups (managed automatically by Kùzu)."""
        pass

    def parse_and_store(
        self,
        target_dir: Path | str,
        db: Optional[Database] = None,
        max_workers: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Scan codebase directory, extract AST data, and write to KùzuDB graph."""
        results = self.scan_directory(target_dir, max_workers=max_workers)
        stats = self.save_to_graph(results, db=db)
        return {
            "target_dir": str(target_dir),
            "files_scanned": len(results),
            "stats": stats,
        }


def parse_codebase(
    target_dir: Path | str,
    db: Optional[Database] = None,
    max_workers: Optional[int] = None,
) -> Dict[str, Any]:
    """Convenience helper to parse a codebase directory and store into KùzuDB."""
    parser = ASTParser()
    return parser.parse_and_store(target_dir, db=db, max_workers=max_workers)
