# pds_parser.py
# Defines the lexical and syntactic structures for Paradox Script (PDS) files.
# Contains:
#   - PdsToken: Represents a lexical token (type, value, line, column).
#   - PdsLexer: Tokenizes PDS text into a stream of PdsTokens.
#               Handles comments, strings, numbers, identifiers, operators, braces.
#   - PdsNode (and subclasses): Abstract Syntax Tree (AST) nodes.
#     - PdsComment, PdsBlankLine, PdsKeyValuePair, PdsList,
#       PdsOperatorCondition, PdsBlock.
#     - Each node implements:
#       - to_string(): For reconstructing PDS text from the AST.
#       - copy(): For deep copying nodes.
#       - get_structural_components(): For equality checks and hashing,
#         defining the "structural essence" of a node.
#       - __eq__, __hash__: Based on structural components.
#   - PdsParser: Parses a stream of PdsTokens into an AST (a list of PdsNodes).
#                Handles top-level statements, block structures, lists, KVP, etc.
#
# Current Known Issues/Limitations:
#   - Lexer: Might not handle all edge cases of PDS syntax, especially complex
#            quoted identifiers or macro expansions if they exist.
#            Assumes UTF-8 with BOM or UTF-8 encoding.
#   - Parser:
#     - List parsing is heuristic (distinguishing `key = { val }` (list) from
#       `key = { child_key = val }` (block)) and might misinterpret ambiguous cases.
#     - Error reporting is basic; doesn't implement sophisticated recovery.
#   - AST Nodes:
#     - `to_string()` formatting is functional but might not perfectly replicate
#       original spacing or stylistic choices beyond basic indentation.
#       The "WARNING: Normalized reconstruction mismatch" messages highlight this.
#     - `PdsList` `to_string()` has heuristics for inline vs. multi-line.
#   - Reconstruction Mismatches: The "WARNING: Normalized reconstruction mismatch"
#     messages indicate that `parser.parse_file()` then `PdsParser._nodes_to_string()`
#     does not perfectly reproduce the original file content after normalization.
#     This is often due to:
#       1. Whitespace differences (e.g., space around '=', number of blank lines).
#       2. Comment positioning nuances not fully captured/reproduced.
#       3. Quoting decisions (e.g., an unquoted identifier in input might be quoted
#          on output if it contains special characters or vice-versa if it doesn't need it).
#     While structural integrity is the main goal for diffing, these cosmetic
#     differences can make direct text diffs of reconstructed files noisy.
#     The `normalize_for_comparison` function in the test script attempts to mitigate this
#     for comparison purposes, but fundamental `to_string` improvements might be needed
#     for perfect 1:1 reconstruction if that's a strict requirement.
import re
import os
import sys

class PdsToken:
    def __init__(self, type, value, line, column):
        self.type = type
        self.value = value
        self.line = line
        self.column = column

    def __repr__(self):
        display_value = self.value
        if isinstance(display_value, str) and len(display_value) > 30:
            display_value = display_value[:27] + "..."
        return f"Token(type='{self.type}', value='{display_value}', line={self.line}, col={self.column})"

class PdsLexer:
    def __init__(self, text):
        self.text = text
        self.pos = 0
        self.line = 1
        self.column = 1
        self.tokens = []
        self.single_char_tokens = {'{': 'LBRACE', '}': 'RBRACE', '=': 'EQUALS'}
        self.patterns = [
            ('COMMENT', r'#.*'),
            ('OPERATOR', r'(?:>=|<=|==|!=|\?=|>|<|!)'),
            ('STRING', r'"(?:\\.|[^"\\])*"'),
            ('NUMBER', r'-?\d+(?:\.\d+)?'),
            ('IDENTIFIER', r'[\w\.:@\-]+(?:[\w\.:@\-]*[\w\.:@\-])?'),
        ]
        self.compiled_patterns = [(k, re.compile(v)) for k, v in self.patterns]

    def _advance_char(self): # Renamed for clarity
        if self.pos < len(self.text):
            char = self.text[self.pos]
            self.pos += 1
            if char == '\n':
                self.line += 1
                self.column = 1
            else:
                self.column += 1
            return char
        return None

    def _add_token(self, token_type, token_value, start_line, start_column):
        self.tokens.append(PdsToken(token_type, token_value, start_line, start_column))

    def tokenize(self):
        while self.pos < len(self.text):
            current_char_start_line = self.line
            current_char_start_column = self.column
            char = self.text[self.pos]

            if char.isspace(): # Includes space, tab, newline
                if char == '\n':
                    # Add NEWLINE token for every newline encountered
                    self._add_token('NEWLINE', '\n', current_char_start_line, current_char_start_column)
                self._advance_char() # Consume the whitespace character
                continue

            if char in self.single_char_tokens:
                self._add_token(self.single_char_tokens[char], char, current_char_start_line, current_char_start_column)
                self._advance_char()
                continue

            matched_pattern = False
            for token_type, pattern_regex in self.compiled_patterns:
                match = pattern_regex.match(self.text, self.pos)
                if match:
                    token_value_raw = match.group(0)
                    val_to_store = token_value_raw
                    if token_type == 'COMMENT':
                        val_to_store = token_value_raw[1:]
                    
                    self._add_token(token_type, val_to_store, current_char_start_line, current_char_start_column)
                    
                    for _ in range(len(token_value_raw)): # Advance lexer position over the matched token
                        self._advance_char()
                    matched_pattern = True
                    break
            
            if not matched_pattern:
                raise ValueError(f"Lexer Error: Unexpected character '{char}' at line {self.line}, column {self.column}")
        
        self._add_token('EOF', '', self.line, self.column) # Add EOF token at the end
        return self.tokens


class PdsNode:
    def __init__(self, line_number=-1):
        self.line_number = line_number
        self.indent_level = 0
        self.comment_text_on_line = None

    def to_string(self, current_indent=0, is_inline_context=False):
        raise NotImplementedError(f"to_string() not implemented for {self.__class__.__name__}")

    def __repr__(self):
        # ... (same as previous version) ...
        key_repr = getattr(self, 'key', 'N/A'); comment_repr = self.comment_text_on_line
        if not isinstance(key_repr, str): key_repr = str(key_repr)
        comment_str = f", Comment='{comment_repr[:10]}...'" if comment_repr else ""
        cl_name = self.__class__.__name__; spec_repr = ""
        if isinstance(self,PdsComment): spec_repr=f"'{getattr(self,'comment_text','')}[:20]...'"
        elif isinstance(self,PdsBlankLine): spec_repr="Blank"
        elif isinstance(self,PdsBlock) or (isinstance(self,PdsKeyValuePair) and isinstance(getattr(self,'value',None),PdsBlock)): spec_repr=f"{key_repr}={{...}}"
        else: spec_repr=f"K='{key_repr}'"
        return f"<{cl_name} L{self.line_number} I{self.indent_level} {spec_repr}{comment_str}>"


    def _base_copy_attrs(self, new_node):
        new_node.line_number = self.line_number
        new_node.indent_level = self.indent_level
        new_node.comment_text_on_line = self.comment_text_on_line
        return new_node

    def copy(self):
        raise NotImplementedError(f"copy() not implemented for {self.__class__.__name__}")

    def get_structural_components(self, shallow_block=False):
        raise NotImplementedError(f"get_structural_components() not implemented for {self.__class__.__name__}")

    def __eq__(self, other):
        if other is None: return False
        if self.__class__ is not other.__class__: return False
        try:
            return self.get_structural_components(shallow_block=False) == \
                   other.get_structural_components(shallow_block=False)
        except NotImplementedError as e:
            sys.stderr.write(f"ERROR: __eq__ called on class without get_structural_components: {e}\n")
            return False

    def __ne__(self, other):
        equal = self.__eq__(other)
        return False if equal is NotImplemented else not equal # Consistent with Python 3

    def __hash__(self):
        try: return hash(self.get_structural_components(shallow_block=False))
        except NotImplementedError: return id(self)

    def get_comparator_key(self, shallow_block_for_seq_matcher=False):
        return self.get_structural_components(shallow_block=shallow_block_for_seq_matcher)

class PdsComment(PdsNode):
    def __init__(self, comment_text, line_number=-1):
        super().__init__(line_number=line_number)
        self.comment_text = comment_text

    def to_string(self, current_indent=0, is_inline_context=False):
        indent_str = " " * current_indent
        return f"{indent_str}#{self.comment_text}\n" # Comments always end with a newline

    def copy(self): # ... (same as previous) ...
        new_node = PdsComment(comment_text=self.comment_text, line_number=self.line_number)
        return self._base_copy_attrs(new_node)

    def get_structural_components(self, shallow_block=False): # ... (same as previous) ...
        return (self.__class__.__name__, self.comment_text)


class PdsBlankLine(PdsNode):
    def __init__(self, line_number=-1):
        super().__init__(line_number=line_number)

    def to_string(self, current_indent=0, is_inline_context=False):
        return "\n" # A blank line node represents one empty line

    def copy(self): # ... (same as previous) ...
        new_node = PdsBlankLine(line_number=self.line_number)
        return self._base_copy_attrs(new_node)

    def get_structural_components(self, shallow_block=False): # ... (same as previous) ...
        return (self.__class__.__name__,)

class PdsKeyValuePair(PdsNode):
    _lexer_identifier_pattern = r'[\w\.:@\-]+(?:[\w\.:@\-]*[\w\.:@\-])?'

    def __init__(self, key, value, line_number=-1, comment_text_on_line=None):
        super().__init__(line_number=line_number)
        self.key = key
        self.value = value
        self.comment_text_on_line = comment_text_on_line

    def to_string(self, current_indent=0, is_inline_context_ignored=False):
        indent_str = " " * current_indent
        line_content_start = f"{indent_str}{self.key} = "
        value_actual_str = ""
        ends_with_newline = True

        if isinstance(self.value, PdsBlock):
            block_node = self.value
            # PdsBlock's to_string needs to know its context for potential inline rendering
            block_render_str = block_node.to_string(current_indent, is_inline_context=True)
            
            if block_render_str.strip().startswith("{") and block_render_str.strip().endswith("}") and "\n" not in block_render_str:
                value_actual_str = block_render_str.strip() # e.g. { child_key = 1 }
            else: # Multiline block value
                # The block's to_string should handle its own indentation starting from current_indent
                # and its own newlines. We just append its output.
                # The lstrip() is important if the block's to_string adds its own initial indent based on current_indent.
                value_actual_str = block_render_str.lstrip() 
                ends_with_newline = False # Block provides its own final newline
        elif isinstance(self.value, str):
            # Quoting logic (same as previous version, ensuring keywords like 'yes'/'no' are not quoted if identifiers)
            is_simple_identifier = re.fullmatch(PdsKeyValuePair._lexer_identifier_pattern, self.value)
            is_keyword_like = self.value.lower() in ["yes", "no", "rgb", "hsv", "hsv360"] # Common unquoted identifiers
            
            needs_quoting = not is_simple_identifier or \
                            any(c in self.value for c in ' \t#={}"') or \
                            not self.value
            
            if is_keyword_like and is_simple_identifier: # yes, no, etc.
                 value_actual_str = self.value
            elif self.key == "icon" and self.value.startswith("@") and is_simple_identifier: # icon = @foo
                 value_actual_str = self.value
            elif needs_quoting:
                escaped_value = self.value.replace('\\', '\\\\').replace('"', '\\"')
                value_actual_str = f'"{escaped_value}"'
            else:
                value_actual_str = self.value
        elif isinstance(self.value, bool):
            value_actual_str = "yes" if self.value else "no"
        else: 
            value_actual_str = str(self.value)
        
        line_content = line_content_start + value_actual_str
        if self.comment_text_on_line: 
            line_content += f" # {self.comment_text_on_line}"
        
        return line_content + ("\n" if ends_with_newline else "")

    def copy(self): # ... (same as previous) ...
        new_value = self.value.copy() if isinstance(self.value, PdsNode) else self.value
        new_node = PdsKeyValuePair(self.key, new_value, self.line_number, self.comment_text_on_line)
        return self._base_copy_attrs(new_node)

    def get_structural_components(self, shallow_block=False): # ... (same as previous) ...
        value_comp = self.value.get_structural_components(shallow_block=shallow_block) if isinstance(self.value, PdsNode) else self.value
        return (self.__class__.__name__, self.key, value_comp, self.comment_text_on_line)


class PdsList(PdsNode): # Key = { val1 "val 2" }
    def __init__(self, key, values, line_number=-1, comment_text_on_line=None):
        super().__init__(line_number=line_number)
        self.key = key
        self.values = values # List of primitives (str, int, float, bool) or PdsNode (e.g. anonymous blocks)
        self.comment_text_on_line = comment_text_on_line

    def to_string(self, current_indent=0, is_inline_context=False):
        indent_str = " " * current_indent
        
        # Heuristic: if all values are simple (not PdsNode) and short, render inline
        all_simple_primitives = all(not isinstance(v, PdsNode) for v in self.values)
        temp_value_str_for_len_check = " ".join([str(v) for v in self.values if not isinstance(v, PdsNode)])

        if all_simple_primitives and (len(self.values) <= 3 or len(temp_value_str_for_len_check) < 50): # Inline
            formatted_values = []
            for v_item in self.values: # Should be primitives here
                if isinstance(v_item, str):
                    is_simple_id = re.fullmatch(PdsKeyValuePair._lexer_identifier_pattern, v_item)
                    needs_q = not is_simple_id or any(c in v_item for c in ' \t#={}"') or not v_item or v_item.lower() in ["yes","no"]
                    if v_item.lower() in ["yes","no"]: formatted_values.append(v_item)
                    elif needs_q: formatted_values.append(f'"{v_item.replace("\\\\", "\\\\\\\\").replace("\"", "\\\"")}"')
                    else: formatted_values.append(v_item)
                elif isinstance(v_item, bool): formatted_values.append("yes" if v_item else "no")
                else: formatted_values.append(str(v_item))
            
            value_str = " ".join(formatted_values)
            inner_content = f" {value_str} " if value_str else " " # Ensure space if empty: key = { }
            base_string = f"{indent_str}{self.key} = {{{inner_content}}}"
            if self.comment_text_on_line: base_string += f" # {self.comment_text_on_line}"
            return base_string + "\n"
        else: # Multi-line: key = {\n  val1\n  val2\n  { anon_block }\n}
            output_lines = []
            open_brace_line = f"{indent_str}{self.key} = {{"
            if self.comment_text_on_line: open_brace_line += f" # {self.comment_text_on_line}"
            output_lines.append(open_brace_line + "\n")

            child_render_indent = current_indent + 4
            for value_item in self.values:
                if isinstance(value_item, PdsNode): # e.g. an anonymous block
                    value_item.indent_level = child_render_indent # Ensure indent before to_string
                    output_lines.append(value_item.to_string(child_render_indent))
                else: # Primitive value, render on its own indented line
                    val_str_item = "" # Copied from inline logic for primitives
                    if isinstance(value_item, str):
                        is_simple_id = re.fullmatch(PdsKeyValuePair._lexer_identifier_pattern, value_item)
                        needs_q = not is_simple_id or any(c in value_item for c in ' \t#={}"') or not value_item or value_item.lower() in ["yes","no"]
                        if value_item.lower() in ["yes","no"]: val_str_item = value_item
                        elif needs_q: val_str_item = f'"{value_item.replace("\\\\", "\\\\\\\\").replace("\"", "\\\"")}"'
                        else: val_str_item = value_item
                    elif isinstance(value_item, bool): val_str_item = "yes" if value_item else "no"
                    else: val_str_item = str(value_item)
                    output_lines.append(f"{' ' * child_render_indent}{val_str_item}\n")
            
            output_lines.append(f"{indent_str}}}\n")
            return "".join(output_lines)

    def copy(self): # ... (same as previous) ...
        new_values = [v.copy() if isinstance(v, PdsNode) else v for v in self.values]
        new_node = PdsList(self.key, new_values, self.line_number, self.comment_text_on_line)
        return self._base_copy_attrs(new_node)

    def get_structural_components(self, shallow_block=False): # ... (same as previous) ...
        values_comp = [v.get_structural_components(shallow_block=shallow_block) if isinstance(v,PdsNode) else v for v in self.values]
        return (self.__class__.__name__, self.key, tuple(values_comp), self.comment_text_on_line)

class PdsOperatorCondition(PdsNode): # key > value
    def __init__(self, key, operator, value, line_number=-1, comment_text_on_line=None):
        super().__init__(line_number=line_number)
        self.key = key; self.operator = operator; self.value = value      
        self.comment_text_on_line = comment_text_on_line

    def to_string(self, current_indent=0, is_inline_context_ignored=False): # Usually inline
        # ... (same as previous, ensure value quoting is robust) ...
        indent_str = " " * current_indent; value_actual_str = ""
        if isinstance(self.value, str):
            is_simple_id = re.fullmatch(PdsKeyValuePair._lexer_identifier_pattern, self.value)
            needs_q = not is_simple_id or any(c in self.value for c in ' \t#={}"') or not self.value or self.value.lower() in ["yes","no"]
            if self.value.lower() in ["yes", "no"]: value_actual_str = self.value
            elif needs_q: value_actual_str = f'"{self.value.replace("\\\\", "\\\\\\\\").replace("\"", "\\\"")}"'
            else: value_actual_str = self.value
        elif isinstance(self.value, bool): value_actual_str = "yes" if self.value else "no"
        elif isinstance(self.value, PdsNode): value_actual_str = self.value.to_string(0).strip()
        else: value_actual_str = str(self.value)
        line = f"{indent_str}{self.key} {self.operator} {value_actual_str}"
        if self.comment_text_on_line: line += f" # {self.comment_text_on_line}"
        return line + "\n"


    def copy(self): # ... (same as previous) ...
        new_value = self.value.copy() if isinstance(self.value, PdsNode) else self.value
        new_node = PdsOperatorCondition(self.key, self.operator, new_value, self.line_number, self.comment_text_on_line)
        return self._base_copy_attrs(new_node)

    def get_structural_components(self, shallow_block=False): # ... (same as previous) ...
        value_comp = self.value.get_structural_components(shallow_block=shallow_block) if isinstance(self.value, PdsNode) else self.value
        return (self.__class__.__name__, self.key, self.operator, value_comp, self.comment_text_on_line)

class PdsBlock(PdsNode):
    def __init__(self, key, line_number=-1, comment_text_on_line=None):
        super().__init__(line_number=line_number)
        self.key = key 
        self.children = []
        self.comment_text_on_line = comment_text_on_line

    def add_child(self, node):
        if not isinstance(node, PdsNode):
            raise TypeError(f"Can only add PdsNode instances as children, got {type(node)}")
        # Indent is set by parser or by to_string based on parent
        self.children.append(node)

    def to_string(self, current_indent=0, is_inline_context=False):
        indent_str = " " * current_indent
        block_key_prefix = f"{self.key} = " if self.key is not None else ""

        # Try inline rendering: key = { child_key = val } (no newlines)
        if (is_inline_context and len(self.children) == 1 and
                isinstance(self.children[0], PdsKeyValuePair) and
                not isinstance(self.children[0].value, PdsBlock) and # Child's value is not another block
                not self.comment_text_on_line and # Block itself has no opening line comment
                not self.children[0].comment_text_on_line # Child has no line comment
                and len(self.children[0].key) + len(str(self.children[0].value)) < 40): # Heuristic for length
            
            # Render child with zero indent relative to its own content, then strip.
            # The child KVP's to_string should not add indent if current_indent is 0.
            child_str_compact = self.children[0].to_string(current_indent=0).strip() # key = value
            return f"{block_key_prefix}{{{child_str_compact}}}" # No newline if truly inline for KVP value

        # Standard multi-line rendering
        output_parts = []
        open_brace_line = f"{indent_str}{block_key_prefix}{{"
        if self.comment_text_on_line:
            open_brace_line += f" # {self.comment_text_on_line}"
        output_parts.append(open_brace_line) # No \n yet

        if not self.children: # Empty block: key = {}
            output_parts.append("}\n") # Add closing brace and newline
        else:
            output_parts.append("\n") # Newline after opening brace if there are children
            child_render_indent = current_indent + 4
            for child_node in self.children:
                child_node.indent_level = child_render_indent # Ensure child knows its indent
                output_parts.append(child_node.to_string(child_render_indent)) # Child.to_string provides its own ending \n
            output_parts.append(f"{indent_str}}}\n") # Indented closing brace with newline
        
        return "".join(output_parts)
        
    # find_child_by_key, find_all_children_by_key, copy, replace_child, remove_child, add_child_at_appropriate_location
    # remain same as previous "good" version.
    def find_child_by_key(self, key_name, Nth=0): # ... (same) ...
        count=0
        for child in self.children:
            if hasattr(child,'key') and child.key==key_name:
                if count==Nth: return child
                count+=1
        return None
    def find_all_children_by_key(self, key_name): # ... (same) ...
        return [child for child in self.children if hasattr(child,'key') and child.key==key_name]
    def copy(self): # ... (same) ...
        new_node=PdsBlock(self.key,self.line_number,self.comment_text_on_line)
        self._base_copy_attrs(new_node)
        new_node.children=[child.copy() for child in self.children]
        return new_node
    def replace_child(self, old_child_node_instance, new_child_node): # ... (same) ...
        if not isinstance(new_child_node, PdsNode): raise TypeError("New child PdsNode")
        try:
            idx=self.children.index(old_child_node_instance)
            new_child_node.indent_level=old_child_node_instance.indent_level
            self.children[idx]=new_child_node; return True
        except ValueError: return False
    def remove_child(self, child_node_instance): # ... (same) ...
        try: self.children.remove(child_node_instance); return True
        except ValueError: return False
    def add_child_at_appropriate_location(self, new_child_node, after_node_instance=None, before_node_instance=None): # ... (same) ...
        if not isinstance(new_child_node, PdsNode): raise TypeError("New child PdsNode")
        new_child_node.indent_level = self.indent_level + 4
        if before_node_instance:
            try: self.children.insert(self.children.index(before_node_instance), new_child_node); return True
            except ValueError: pass 
        if after_node_instance:
            try: self.children.insert(self.children.index(after_node_instance) + 1, new_child_node); return True
            except ValueError: pass
        insert_idx = len(self.children)
        for i in reversed(range(len(self.children))):
            if not isinstance(self.children[i], (PdsComment, PdsBlankLine)): insert_idx=i+1; break
        self.children.insert(insert_idx, new_child_node); return True


    def get_structural_components(self, shallow_block=False): # ... (same as previous) ...
        if shallow_block: return (self.__class__.__name__, self.key, self.comment_text_on_line)
        else:
            children_comps = tuple(c.get_structural_components(shallow_block=False) for c in self.children)
            return (self.__class__.__name__, self.key, self.comment_text_on_line, children_comps)

class PdsParser:
    def __init__(self):
        self.tokens = []; self.current_token_index = 0
        self.root_nodes = []; self.parent_stack = []

    def _error(self, message, token_override=None):
        # ... (same as previous version) ...
        token_info = ""; token_to_report = token_override if token_override else self._peek()
        if token_to_report and token_to_report.type != 'EOF': token_info = f"at token '{token_to_report.value}' (type: {token_to_report.type}) on line {token_to_report.line}, col {token_to_report.column}"
        elif self.current_token_index > 0 and self.current_token_index <= len(self.tokens): prev_token = self.tokens[self.current_token_index -1]; token_info = f"after token '{prev_token.value}' (type: {prev_token.type}) on line {prev_token.line}, col {prev_token.column}"
        else: token_info = "at beginning/empty file"
        raise ValueError(f"Parser Error: {message}. {token_info}")

    def _peek(self, offset=0): # ... (same) ...
        idx = self.current_token_index + offset
        return self.tokens[idx] if idx < len(self.tokens) else self.tokens[-1]

    def _advance(self): # ... (same) ...
        if self.current_token_index < len(self.tokens) - 1: self.current_token_index += 1
        return self.tokens[self.current_token_index -1]

    def _consume(self, *expected_types): # ... (same) ...
        token = self._peek()
        if token.type in expected_types: return self._advance()
        self._error(f"Expected one of {expected_types}", token_override=token)

    def _is_next(self, *token_types): return self._peek().type in token_types # ... (same) ...
    def is_eof(self): return self._peek().type == 'EOF' # ... (same) ...

    def _parse_comment_node(self): # ... (same) ...
        token = self._consume('COMMENT'); return PdsComment(token.value, token.line)

    def _parse_primitive_value(self, token_val_if_consumed=None): # ... (same as previous, ensure it handles consumed tokens) ...
        token_obj = None
        if token_val_if_consumed is None: token_obj = self._peek()
        
        val_to_parse = token_val_if_consumed if token_val_if_consumed is not None else token_obj.value
        type_of_val = 'IDENTIFIER' # Assume if pre-consumed; parser needs to know this from context
        if token_obj: type_of_val = token_obj.type
        
        if type_of_val == 'IDENTIFIER':
            if token_obj: self._advance() # Consume if not pre-consumed
            if val_to_parse.lower() == "yes": return True
            if val_to_parse.lower() == "no": return False
            return val_to_parse 
        elif type_of_val == 'NUMBER':
            if token_obj: self._advance()
            try: return float(val_to_parse) if '.' in val_to_parse else int(val_to_parse)
            except ValueError: return val_to_parse 
        elif type_of_val == 'STRING':
            if token_obj: self._advance()
            val_str = val_to_parse[1:-1]; return val_str.replace('\\"', '"').replace('\\\\', '\\')
        return None


    def _parse_value_for_kvp_or_op(self): # ... (same as previous) ...
        if self._is_next('LBRACE'): return self._parse_block_content(key_for_block=None, line_of_key=self._peek().line)
        else:
            pv = self._parse_primitive_value();
            if pv is not None: return pv
            self._error(f"Expected a value (primitive or anonymous block {{...}})")
        return None

    def _parse_statement_or_item(self): # Handles KVP, OpCond, Block, or bare value/anon block for list
        start_token_line = self._peek().line
        if self._is_next('COMMENT'): return self._parse_comment_node()
        if self._is_next('LBRACE'): return self._parse_block_content(key_for_block=None, line_of_key=start_token_line) # Anonymous block

        if not (self._is_next('IDENTIFIER') or self._is_next('NUMBER') or self._is_next('STRING')):
             self._error("Statement or list item must start with IDENTIFIER, NUMBER, STRING, or LBRACE")

        key_cand_token = self._consume('IDENTIFIER', 'NUMBER', 'STRING')
        key_str_raw = key_cand_token.value; key_line = key_cand_token.line
        key_for_node = key_str_raw
        if key_cand_token.type == 'STRING': key_for_node = key_str_raw[1:-1].replace('\\"', '"').replace('\\\\', '\\')

        if self._is_next('EQUALS'):
            self._consume('EQUALS')
            if self._is_next('LBRACE'): return self._parse_block_or_list_after_equals(key_for_node, key_line)
            else:
                val_node = self._parse_primitive_value()
                if val_node is None: self._error("Expected primitive value after '='")
                cmt = None
                if self._is_next('COMMENT') and self._peek().line == self.tokens[self.current_token_index-1].line:
                    cmt = self._consume('COMMENT').value
                return PdsKeyValuePair(key_for_node, val_node, key_line, cmt)
        elif self._is_next('OPERATOR'):
            op_tok = self._consume('OPERATOR'); val_node = self._parse_value_for_kvp_or_op(); cmt = None
            if self._is_next('COMMENT') and self._peek().line == self.tokens[self.current_token_index-1].line:
                 cmt = self._consume('COMMENT').value
            return PdsOperatorCondition(key_for_node, op_tok.value, val_node, key_line, cmt)
        elif self._is_next('LBRACE'): return self._parse_block_content(key_for_node, key_line)
        else: # Bare value (for list)
            return self._parse_primitive_value(token_val_if_consumed=key_str_raw)


    def _parse_block_or_list_after_equals(self, key_str_for_node, key_line_for_node):
        # ... (same as previous refined version, with robust lookahead) ...
        lbrace_token = self._consume('LBRACE'); comment_on_lbrace = None
        if self._is_next('COMMENT') and self._peek().line == lbrace_token.line: comment_on_lbrace = self._consume('COMMENT').value
        is_likely_block = False; temp_idx = self.current_token_index
        while temp_idx < len(self.tokens) and self.tokens[temp_idx].type in ('NEWLINE','COMMENT'): temp_idx +=1
        if temp_idx < len(self.tokens):
            first_sig_tok = self.tokens[temp_idx]
            if first_sig_tok.type in ('IDENTIFIER','STRING','NUMBER'):
                if temp_idx + 1 < len(self.tokens) and self.tokens[temp_idx+1].type in ('EQUALS','OPERATOR','LBRACE'): is_likely_block=True
            # If first_sig_tok is LBRACE, it's a list of anonymous blocks, so NOT is_likely_block (for PdsBlock itself)
        if self._is_next('RBRACE'): is_likely_block = True # Empty {} is a block
        
        if is_likely_block:
            block_node = self._parse_block_content(key_str_for_node, key_line_for_node, block_opening_comment=comment_on_lbrace, is_rhs_of_equals=True)
            return block_node
        else:
            list_node = PdsList(key_str_for_node, [], key_line_for_node, comment_on_lbrace)
            while not self._is_next('RBRACE') and not self.is_eof():
                self._skip_newlines_and_comments_in_list()
                if self._is_next('RBRACE') or self.is_eof(): break
                list_item = self._parse_statement_or_item()
                if list_item is not None: list_node.values.append(list_item)
                elif not self._is_next('RBRACE'): self._error("Expected value or RBRACE in list")
            self._consume('RBRACE'); return list_node
            
    def _skip_newlines_and_comments_in_list(self): # ... (same as previous) ...
        while self._is_next('NEWLINE') or self._is_next('COMMENT'):
            self._advance()
            if self.is_eof(): break


    def _parse_block_content(self, key_for_block, line_of_key, block_opening_comment=None, is_rhs_of_equals=False):
        if not is_rhs_of_equals:
            lbrace_token = self._consume('LBRACE')
            if self._is_next('COMMENT') and self._peek().line == lbrace_token.line:
                 block_opening_comment = self._consume('COMMENT').value
        
        block_node = PdsBlock(key_for_block, line_of_key, block_opening_comment)
        current_parent_indent = self.parent_stack[-1].indent_level if self.parent_stack else -4
        block_node.indent_level = current_parent_indent + 4
        self.parent_stack.append(block_node)

        # Consume initial newlines after LBRACE, but don't make them blank line nodes yet.
        # A PdsBlankLine node represents a *semantically empty line* between statements.
        processed_a_child_on_this_line = False # To help decide if a newline is a blank line
        
        while not self._is_next('RBRACE') and not self.is_eof():
            current_token = self._peek()
            
            if current_token.type == 'NEWLINE':
                self._advance() # Consume this newline
                # If the next token is also a NEWLINE (on a different line effectively)
                # or RBRACE or EOF, then the consumed newline formed a blank line.
                next_peek = self._peek()
                if (next_peek.type == 'NEWLINE' and next_peek.line != current_token.line) or \
                   next_peek.type == 'RBRACE' or next_peek.type == 'EOF':
                    # Only add if there wasn't content on the line that just ended with current_token (NEWLINE)
                    # This simple check might still create too many if not careful.
                    # A better check: was the line of `current_token` truly blank before it?
                    # For now, if we just consumed a NEWLINE, and what follows implies it was an empty line:
                    block_node.add_child(PdsBlankLine(current_token.line))
                processed_a_child_on_this_line = False # Newline resets this
                continue 
            
            processed_a_child_on_this_line = True

            if current_token.type == 'COMMENT':
                block_node.add_child(self._parse_comment_node())
                # A comment is a statement, so the line isn't blank before it for blank line logic
                # After a comment, a newline will follow, handled by next iteration.
                continue
            
            if self.is_eof(): self._error("Unexpected EOF inside block", token_override=self._peek(-1))
            if self._is_next('RBRACE'): break # Should be caught by while loop condition

            child_node = self._parse_statement_or_item()
            if child_node is not None:
                 if not isinstance(child_node, PdsNode):
                     self._error(f"Block '{key_for_block}' cannot directly contain primitive value '{child_node}'.")
                 block_node.add_child(child_node)
            elif not self._is_next('RBRACE') and not self.is_eof():
                 self._error(f"Parser stuck in block '{key_for_block}' before token", token_override=self._peek())
        
        self._consume('RBRACE')
        self.parent_stack.pop()
        return block_node

    def parse_file(self, filepath):
        # ... (file reading and lexer setup - same as previous) ...
        self.root_nodes = []; self.parent_stack = []; self.current_token_index = 0
        # ... (error handling for file ops - same) ...
        if not os.path.exists(filepath): sys.stderr.write(f"Error: File not found: {filepath}\n"); return []
        try:
            with open(filepath, 'r', encoding='utf-8-sig') as f: text_content = f.read()
        except UnicodeDecodeError:
            with open(filepath, 'r', encoding='utf-8') as f: text_content = f.read()
        except Exception as e: sys.stderr.write(f"Error reading {filepath}: {e}\n"); return []
        
        lexer = PdsLexer(text_content)
        try: self.tokens = lexer.tokenize()
        except ValueError as lex_err: sys.stderr.write(f"Lexer error in {filepath}: {lex_err}\n"); return []
        if not self.tokens or self.tokens[0].type == 'EOF': return []


        while not self.is_eof():
            current_token = self._peek()
            if current_token.type == 'NEWLINE':
                self._advance()
                next_peek = self._peek()
                if (next_peek.type == 'NEWLINE' and next_peek.line != current_token.line) or \
                   next_peek.type == 'EOF':
                    self.root_nodes.append(PdsBlankLine(current_token.line))
                continue
            
            if current_token.type == 'COMMENT':
                self.root_nodes.append(self._parse_comment_node())
                continue
            
            if self.is_eof(): break

            try:
                node = self._parse_statement_or_item()
                if node:
                    if not isinstance(node, (PdsKeyValuePair, PdsOperatorCondition, PdsBlock, PdsList, PdsComment, PdsBlankLine)): # PdsList added
                        self._error(f"Root level statement parsed into unexpected type: {type(node)}. Value: {node}")
                    self.root_nodes.append(node)
                elif not self.is_eof(): self._error(f"Parser did not produce a node and is not at EOF.")
            except ValueError as parse_err:
                sys.stderr.write(f"Parser error in {filepath}: {parse_err}\n")
                return self.root_nodes 
        return self.root_nodes

    @staticmethod
    def _nodes_to_string(nodes_list):
        # ... (same as previous good version) ...
        output = []
        for node in nodes_list:
            node_indent = node.indent_level if node.indent_level is not None else 0
            output.append(node.to_string(node_indent)) 
        return "".join(output)


    def to_string(self): return PdsParser._nodes_to_string(self.root_nodes)