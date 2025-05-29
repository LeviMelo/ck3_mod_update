# pds_parser.py - MODIFIED FOR TO_STRING FIXES

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

    def _advance_char(self):
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

            if char.isspace():
                if char == '\n':
                    self._add_token('NEWLINE', '\n', current_char_start_line, current_char_start_column)
                self._advance_char()
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
                        val_to_store = token_value_raw[1:] # Store comment text without '#'
                    
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
        self.comment_text_on_line = None # For comments on the same line as the node's definition

    # MODIFIED: Added `is_value` parameter
    def to_string(self, current_indent=0, is_inline_context=False, is_value=False):
        raise NotImplementedError(f"to_string() not implemented for {self.__class__.__name__}")

    def __repr__(self):
        key_repr = getattr(self, 'key', 'N/A')
        comment_repr = self.comment_text_on_line
        if not isinstance(key_repr, str): key_repr = str(key_repr) # Handle non-string keys like numbers
        comment_str = f", Comment='{comment_repr[:20]}...'" if comment_repr and len(comment_repr) > 20 else (f", Comment='{comment_repr}'" if comment_repr else "")
        
        cl_name = self.__class__.__name__
        spec_repr = ""
        if isinstance(self,PdsComment): spec_repr=f"'{getattr(self,'comment_text','')[:20]}...'" if len(getattr(self,'comment_text','')) > 20 else f"'{getattr(self,'comment_text','')}'"
        elif isinstance(self,PdsBlankLine): spec_repr="Blank"
        elif isinstance(self,PdsBlock) or (isinstance(self,PdsKeyValuePair) and isinstance(getattr(self,'value',None),PdsBlock)):
             spec_repr=f"{key_repr}={{...}}"
        elif isinstance(self,PdsList) or (isinstance(self,PdsKeyValuePair) and isinstance(getattr(self,'value',None),PdsList)):
             spec_repr=f"{key_repr}={{...List...}}"
        elif isinstance(self,PdsOperatorCondition): spec_repr=f"{key_repr} {getattr(self,'operator','')} {getattr(self,'value','?!')}"
        else: spec_repr=f"K='{key_repr}'" # Fallback for KVP (primitive value)
        return f"<{cl_name} L{self.line_number} I{self.indent_level} {spec_repr}{comment_str}>"


    def _base_copy_attrs(self, new_node):
        new_node.line_number = self.line_number
        new_node.indent_level = self.indent_level
        new_node.comment_text_on_line = self.comment_text_on_line
        return new_node

    def copy(self):
        raise NotImplementedError(f"copy() not implemented for {self.__class__.__name__}")

    def get_structural_components(self, shallow_block=False):
        # Base implementation returns class name and line number as a fallback,
        # but should be overridden by all concrete node types.
        raise NotImplementedError(f"get_structural_components() not implemented for {self.__class__.__name__}")

    def __eq__(self, other):
        if other is None: return False
        if self.__class__ is not other.__class__: return False
        try:
            # Ensure comparison is always deep unless shallow_block is explicitly True where supported
            return self.get_structural_components(shallow_block=False) == \
                   other.get_structural_components(shallow_block=False)
        except NotImplementedError as e:
            sys.stderr.write(f"ERROR: __eq__ called on class without get_structural_components: {e}\n")
            return False

    def __ne__(self, other):
        equal = self.__eq__(other)
        return False if equal is NotImplemented else not equal

    def __hash__(self):
        try:
            # Ensure hash is always deep unless shallow_block is explicitly True where supported
            return hash(self.get_structural_components(shallow_block=False))
        except NotImplementedError:
            return id(self)

    def get_comparator_key(self, shallow_block_for_seq_matcher=False):
        # This is the key used by difflib.SequenceMatcher.
        # By default, it's the full structural components, unless shallow_block_for_seq_matcher is requested.
        return self.get_structural_components(shallow_block=shallow_block_for_seq_matcher)

class PdsComment(PdsNode):
    def __init__(self, comment_text, line_number=-1):
        super().__init__(line_number=line_number)
        self.comment_text = comment_text # Does not include the '#'

    # MODIFIED: Added `is_value` parameter (ignored)
    def to_string(self, current_indent=0, is_inline_context=False, is_value=False):
        indent_str = " " * current_indent
        return f"{indent_str}#{self.comment_text}\n" # Comments always end with a newline

    def copy(self):
        new_node = PdsComment(comment_text=self.comment_text, line_number=self.line_number)
        return self._base_copy_attrs(new_node)

    def get_structural_components(self, shallow_block=False):
        return (self.__class__.__name__, self.comment_text)


class PdsBlankLine(PdsNode):
    def __init__(self, line_number=-1):
        super().__init__(line_number=line_number)

    # MODIFIED: Added `is_value` parameter (ignored)
    def to_string(self, current_indent=0, is_inline_context=False, is_value=False):
        return "\n" # A blank line node represents one empty line

    def copy(self):
        new_node = PdsBlankLine(line_number=self.line_number)
        return self._base_copy_attrs(new_node)

    def get_structural_components(self, shallow_block=False):
        return (self.__class__.__name__,) # All blank lines are structurally identical for comparison

class PdsKeyValuePair(PdsNode):
    # Pattern for unquoted identifiers: must be alphanumeric, underscore, dot, colon, at-sign, hyphen.
    # It must *fully* match the string to be considered an unquoted identifier.
    _lexer_identifier_pattern = re.compile(r'[\w\.:@\-]+')

    def __init__(self, key, value, line_number=-1, comment_text_on_line=None):
        super().__init__(line_number=line_number)
        self.key = key # string or number (parsed from IDENTIFIER or NUMBER token)
        # Value can be primitive, PdsBlock, or PdsList
        self.value = value
        self.comment_text_on_line = comment_text_on_line

    # MODIFIED: Added `is_value` parameter, added PdsList handling, refined Block handling
    def to_string(self, current_indent=0, is_inline_context=False, is_value=False):
        indent_str = " " * current_indent
        key_actual_str = str(self.key)

        line_content_start = f"{indent_str}{key_actual_str} = "
        value_actual_str = ""
        ends_with_newline = True # Default for primitive values

        if isinstance(self.value, PdsBlock):
            # Pass current_indent to the block's to_string for proper indentation *within* the block
            # Pass is_inline_context hint, and crucially, pass is_value=True
            block_render_str = self.value.to_string(current_indent, is_inline_context=True, is_value=True)

            # Check if the block rendered itself inline (no newlines except potentially at end)
            if "\n" not in block_render_str.strip(): # Check after stripping leading/trailing whitespace
                value_actual_str = block_render_str.strip() # Use the stripped version for inline KVP line
            else: # Multiline block value
                value_actual_str = block_render_str # Block's to_string handles its own newlines and indentation.
                ends_with_newline = False # The block itself will add the final newline
        # ADDED: Handle PdsList value explicitly
        elif isinstance(self.value, PdsList):
             # Similar logic as PdsBlock value
            list_render_str = self.value.to_string(current_indent, is_inline_context=True, is_value=True)

            # Check if the list rendered itself inline
            if "\n" not in list_render_str.strip():
                 value_actual_str = list_render_str.strip()
            else: # Multiline list value
                value_actual_str = list_render_str
                ends_with_newline = False

        elif isinstance(self.value, str):
            is_special_unquoted_keyword = self.value.lower() in ["yes", "no", "rgb", "hsv", "hsv360", "root", "prev", "owner"]
            is_icon_path_literal = self.key == "icon" and self.value.startswith("@")
            is_simple_identifier_fully = self._lexer_identifier_pattern.fullmatch(self.value) is not None

            # Determine if quotes are needed:
            needs_quoting = False
            # Quote if it's not a simple identifier (e.g., contains spaces or forbidden chars)
            if not is_simple_identifier_fully:
                needs_quoting = True
            # Quote if it's an empty string ""
            elif not self.value:
                needs_quoting = True
            # Special keywords/paths generally don't need quoting unless they also contain forbidden chars (rare)
            if is_special_unquoted_keyword or is_icon_path_literal:
                needs_quoting = False

            if needs_quoting:
                # Escape backslashes and double quotes
                escaped_value = self.value.replace('\\', '\\\\').replace('"', '\\"')
                value_actual_str = f'"{escaped_value}"'
            else:
                value_actual_str = self.value
        elif isinstance(self.value, bool):
            value_actual_str = "yes" if self.value else "no"
        elif isinstance(self.value, PdsNode): # Should ideally not happen if not Block or List, error or unhandled node type as value
             # This fallback might indicate an unexpected node type as a KVP value.
             # Render it as a string for robustness, but might not be correct syntax.
             # Pass is_value=True to the value node's to_string.
             value_actual_str = self.value.to_string(0, is_inline_context=True, is_value=True).strip() # Render with 0 indent relative to value start
             # A node value (if not Block/List) rendering inline might not end with newline
             ends_with_newline = ("\n" not in value_actual_str) # Assume if it contains newline it handles it.
             # This fallback needs careful review if other node types are used as KVP values.
             # For PDS, usually only primitives, blocks, or lists are KVP values.

        else: # Numbers (int, float) or other primitive types
            value_actual_str = str(self.value)
            ends_with_newline = True # Primitives usually end the line

        line_content = line_content_start + value_actual_str
        if self.comment_text_on_line:
            line_content += f" # {self.comment_text_on_line}"

        # If the value was a multiline Block/List, its to_string includes the final newline.
        # If it was a primitive or inline node value, we add the newline here.
        return line_content + ("\n" if ends_with_newline else "")


    def copy(self):
        # Primitives are copied by value. PdsNode instances need deep copy.
        new_value = self.value.copy() if isinstance(self.value, PdsNode) else self.value
        new_node = PdsKeyValuePair(self.key, new_value, self.line_number, self.comment_text_on_line)
        return self._base_copy_attrs(new_node)

    def get_structural_components(self, shallow_block=False):
        # If value is a PdsNode (Block, List, etc.), its structural components depend on 'shallow_block' recursion
        value_comp = None
        if isinstance(self.value, PdsNode):
            # Pass shallow_block flag when recursing into value node's components
            value_comp = self.value.get_structural_components(shallow_block=shallow_block)
        else: # Primitive value
            value_comp = self.value
        # Include key type for robustness in comparison, e.g., "10" string key != 10 int key
        key_comp = (type(self.key).__name__, self.key)
        return (self.__class__.__name__, key_comp, value_comp, self.comment_text_on_line)

class PdsList(PdsNode): # Represents key = { val1 "val 2" ... }
    def __init__(self, key, values, line_number=-1, comment_text_on_line=None):
        super().__init__(line_number=line_number)
        self.key = key # string or number
        self.values = values # List of primitives (str, int, float, bool) or PdsNode (e.g. anonymous blocks/lists, KVPs if list contains structured items)
        self.comment_text_on_line = comment_text_on_line

    # MODIFIED: Added `is_value` parameter and logic
    def to_string(self, current_indent=0, is_inline_context=False, is_value=False):
        indent_str = " " * current_indent

        # Determine the prefix: "key = " if it's not a value AND has a key
        list_key_prefix = ""
        if not is_value and self.key is not None:
             list_key_prefix = f"{str(self.key)} = "


        # Heuristic for inline vs. multi-line:
        # Multi-line if: any value is a PdsNode, list has many values, combined string length exceeds threshold.
        # Otherwise, attempt inline. Empty lists are always inline.
        is_multiline = False
        if any(isinstance(v, PdsNode) for v in self.values):
            is_multiline = True
        elif len(self.values) > 5: # Arbitrary threshold
            is_multiline = True
        # Estimate inline length (only for primitives as nodes are always multiline anyway)
        elif len(self.values) > 0 and len(" ".join([str(v) for v in self.values if not isinstance(v, PdsNode)])) > 70: # Arbitrary threshold
            is_multiline = True

        if not self.values: # Empty list is always inline
            is_multiline = False
        # If it's a value context and inline is possible, force inline (e.g. kvp = { val } )
        elif is_value and is_inline_context:
             is_multiline = False # Re-evaluate based on value count/type? For now, favor inline if value context hints it.
             # Re-check if any node requires multiline rendering even in inline context
             if any(isinstance(v, PdsNode) and not v.to_string(0, is_inline_context=True, is_value=True).strip() for v in self.values):
                  is_multiline = True # If any child node cannot render inline, the list must be multiline.


        if not is_multiline: # Inline format: key = { val1 val2 "val 3" }
            formatted_values = []
            for v_item in self.values:
                val_str_item = ""
                if isinstance(v_item, str):
                    # Use PdsKeyValuePair's robust quoting logic
                    is_simple_id = PdsKeyValuePair._lexer_identifier_pattern.fullmatch(v_item) is not None
                    is_bool_like_keyword = v_item.lower() in ["yes", "no"]
                    needs_q = not is_simple_id or any(c in v_item for c in ' \t#={}"') or not v_item
                    if is_bool_like_keyword and is_simple_id and not needs_q: val_str_item = v_item
                    elif needs_q: val_str_item = f'"{v_item.replace("\\", "\\\\").replace("\"", "\\\"")}"'
                    else: val_str_item = v_item
                elif isinstance(v_item, bool):
                    val_str_item = "yes" if v_item else "no"
                elif isinstance(v_item, PdsNode): # Handle nodes inside inline lists (e.g. anonymous blocks)
                     # Render node inline with no extra indent, pass is_value=True as it's a value within the list
                     val_str_item = v_item.to_string(0, is_inline_context=True, is_value=True).strip()
                else: # Numbers
                    val_str_item = str(v_item)
                formatted_values.append(val_str_item)

            value_str = " ".join(formatted_values)
            inner_content = f" {value_str} " if value_str else " " # Add space inside braces even if empty list
            base_string = f"{indent_str}{list_key_prefix}{{{inner_content}}}"
            if self.comment_text_on_line:
                base_string += f" # {self.comment_text_on_line}"
            return base_string + "\n" # Inline lists usually end with a newline

        else: # Multi-line format
            output_lines = []
            open_brace_line = f"{indent_str}{list_key_prefix}{{"
            if self.comment_text_on_line:
                open_brace_line += f" # {self.comment_text_on_line}"
            output_lines.append(open_brace_line + "\n")

            child_render_indent = current_indent + 4
            for value_item in self.values:
                if isinstance(value_item, PdsNode):
                    # Pass is_inline_context=False for children of multi-line list.
                    # Children nodes are not values of the *list* node itself, but statements *within* the list structure.
                    # So, is_value=False here.
                    output_lines.append(value_item.to_string(child_render_indent, is_inline_context=False, is_value=False))
                else: # Primitive value
                    val_str_item = ""
                    if isinstance(value_item, str):
                        is_simple_id = PdsKeyValuePair._lexer_identifier_pattern.fullmatch(value_item) is not None
                        is_bool_like_keyword = value_item.lower() in ["yes", "no"]
                        needs_q = not is_simple_id or any(c in value_item for c in ' \t#={}"') or not value_item
                        if is_bool_like_keyword and is_simple_id and not needs_q: val_str_item = value_item
                        elif needs_q: val_str_item = f'"{value_item.replace("\\", "\\\\").replace("\"", "\\\"")}"'
                        else: val_str_item = value_item
                    elif isinstance(value_item, bool): val_str_item = "yes" if value_item else "no"
                    else: val_str_item = str(value_item)
                    # Primitive values also get indented in multi-line lists
                    output_lines.append(f"{' ' * child_render_indent}{val_str_item}\n")

            output_lines.append(f"{indent_str}}}\n") # Closing brace at parent's indent level
            return "".join(output_lines)


    def copy(self):
        new_values = [v.copy() if isinstance(v, PdsNode) else v for v in self.values]
        new_node = PdsList(self.key, new_values, self.line_number, self.comment_text_on_line)
        return self._base_copy_attrs(new_node)

    def get_structural_components(self, shallow_block=False):
        values_comp = []
        for v_item in self.values:
            if isinstance(v_item, PdsNode):
                # Pass shallow_block flag when recursing into node value's components
                values_comp.append(v_item.get_structural_components(shallow_block=shallow_block))
            else: # Primitive
                values_comp.append(v_item)
        # Include key type for robustness
        key_comp = (type(self.key).__name__, self.key) if self.key is not None else (type(None).__name__, None) # Handle lists with None key (anonymous)
        return (self.__class__.__name__, key_comp, tuple(values_comp), self.comment_text_on_line)

class PdsOperatorCondition(PdsNode): # e.g. key > value, or trigger_value = { limit = { key > value } }
    def __init__(self, key, operator, value, line_number=-1, comment_text_on_line=None):
        super().__init__(line_number=line_number)
        self.key = key # string or number (LHS)
        self.operator = operator # string (e.g., ">", "<=", "==")
        self.value = value # primitive or PdsNode (RHS)
        self.comment_text_on_line = comment_text_on_line

    # MODIFIED: Added `is_value` parameter, pass is_value=True to value's to_string
    def to_string(self, current_indent=0, is_inline_context=False, is_value=False):
        indent_str = " " * current_indent
        key_actual_str = str(self.key) # Assuming keys don't need complex quoting here

        value_actual_str = ""
        # RHS value formatting (similar to KVP's value formatting)
        if isinstance(self.value, PdsNode):
             # Pass is_value=True to the value node's to_string as it is the RHS value
             value_actual_str = self.value.to_string(0, is_inline_context=True, is_value=True).strip() # Render value with 0 relative indent, strip newline
        elif isinstance(self.value, str):
            is_simple_id = re.fullmatch(PdsKeyValuePair._lexer_identifier_pattern, self.value)
            is_keyword_like = self.value.lower() in ["yes", "no", "root", "prev", "owner"] # Common unquoted RHS identifiers
            needs_q = not is_simple_id or any(c in self.value for c in ' \t#={}"') or not self.value
            if is_keyword_like and is_simple_id and not needs_q: value_actual_str = self.value
            elif needs_q: value_actual_str = f'"{self.value.replace("\\\\", "\\\\\\\\").replace("\"", "\\\"")}"'
            else: value_actual_str = self.value
        elif isinstance(self.value, bool):
            value_actual_str = "yes" if self.value else "no"
        else: # Numbers
            value_actual_str = str(self.value)

        line = f"{indent_str}{key_actual_str} {self.operator} {value_actual_str}"
        if self.comment_text_on_line:
            line += f" # {self.comment_text_on_line}"
        return line + "\n" # Operator conditions are usually standalone statements ending in newline


    def copy(self):
        new_value = self.value.copy() if isinstance(self.value, PdsNode) else self.value
        new_node = PdsOperatorCondition(self.key, self.operator, new_value, self.line_number, self.comment_text_on_line)
        return self._base_copy_attrs(new_node)

    def get_structural_components(self, shallow_block=False):
        value_comp = self.value.get_structural_components(shallow_block=shallow_block) if isinstance(self.value, PdsNode) else self.value
        key_comp = (type(self.key).__name__, self.key) # Include key type for robustness
        return (self.__class__.__name__, key_comp, self.operator, value_comp, self.comment_text_on_line)

class PdsBlock(PdsNode): # Represents key = { ...children... } or anonymous { ...children... }
    def __init__(self, key, line_number=-1, comment_text_on_line=None): # Key can be None for anonymous blocks
        super().__init__(line_number=line_number)
        self.key = key # string, number, or None
        self.children = [] # List of PdsNode instances
        self.comment_text_on_line = comment_text_on_line # Comment on the opening brace line: foo = { # THIS

    def add_child(self, node):
        if not isinstance(node, PdsNode):
            raise TypeError(f"Can only add PdsNode instances as children to PdsBlock, got {type(node)}")
        self.children.append(node)

    # MODIFIED: Added `is_value` parameter and logic
    def to_string(self, current_indent=0, is_inline_context=False, is_value=False):
        indent_str = " " * current_indent
        block_key_prefix = ""
        # Only include "key = " if this block is NOT a value of a KVP/OpCond AND has a key
        if not is_value and self.key is not None:
            block_key_prefix = f"{str(self.key)} = "

        # Heuristic for inline rendering: key = { child_key = val }
        # This is primarily for blocks that are values of KVPs (is_inline_context=True and is_value=True).
        # Allow comments if the inline form is still compact.
        # Only attempt inline rendering if rendering as a value AND in an inline context AND specific child pattern
        can_attempt_inline_render = is_value and is_inline_context and len(self.children) == 1 and \
                                   isinstance(self.children[0], PdsKeyValuePair) and \
                                   not isinstance(self.children[0].value, (PdsBlock, PdsList)) and \
                                   len(str(self.children[0].key)) + len(str(self.children[0].value)) < 80 # Arbitrary length threshold

        if can_attempt_inline_render:
            child_kvp_node = self.children[0]
            # Render child KVP with 0 indent relative to the block's internal content,
            # indicate it's in an inline context, and it's NOT a value of the block itself.
            child_str_compact = child_kvp_node.to_string(current_indent=0, is_inline_context=True, is_value=False).strip()

            # If the child KVP's string form is truly a single line (no internal newlines), use it.
            if "\n" not in child_str_compact:
                # The output for an inline block value is "{ child_content }" or "key = { child_content }" if not a value
                block_output = f"{block_key_prefix}{{{child_str_compact}}}"
                if self.comment_text_on_line:
                    block_output += f" # {self.comment_text_on_line}"
                return block_output # No trailing newline for inline rendering

        # Standard multi-line rendering
        output_parts = []
        # The opening brace line includes the key prefix only if not rendering as a value
        open_brace_line = f"{indent_str}{block_key_prefix}{{"
        if self.comment_text_on_line:
            open_brace_line += f" # {self.comment_text_on_line}"
        output_parts.append(open_brace_line)

        if not self.children: # Empty block
            output_parts.append("}\n") # Closing brace immediately after opening, followed by newline
        else: # Block with children
            output_parts.append("\n") # Newline after opening brace
            child_render_indent = current_indent + 4
            for child_node in self.children:
                # Children nodes are statements *within* the block, not values of the block node itself.
                output_parts.append(child_node.to_string(child_render_indent, is_inline_context=False, is_value=False))
            output_parts.append(f"{indent_str}}}\n") # Closing brace at parent block's indent level

        return "".join(output_parts)

    def find_child_by_key(self, key_name, Nth=0):
        count = 0
        for child in self.children:
            if hasattr(child, 'key') and child.key == key_name:
                if count == Nth:
                    return child
                count += 1
        return None

    def find_all_children_by_key(self, key_name):
        return [child for child in self.children if hasattr(child, 'key') and child.key == key_name]

    def copy(self):
        new_node = PdsBlock(self.key, self.line_number, self.comment_text_on_line)
        self._base_copy_attrs(new_node) # Copies line_number, indent_level, comment_text_on_line
        new_node.children = [child.copy() for child in self.children]
        return new_node

    def replace_child(self, old_child_node_instance, new_child_node):
        if not isinstance(new_child_node, PdsNode):
            raise TypeError("New child must be a PdsNode instance.")
        try:
            idx = self.children.index(old_child_node_instance)
            # Preserve indent level of the original child node's position for formatting
            new_child_node.indent_level = old_child_node_instance.indent_level
            self.children[idx] = new_child_node
            return True
        except ValueError: # old_child_node_instance not found
            return False

    def remove_child(self, child_node_instance):
        try:
            self.children.remove(child_node_instance)
            return True
        except ValueError:
            return False

    def add_child_at_appropriate_location(self, new_child_node, after_node_instance=None, before_node_instance=None):
        if not isinstance(new_child_node, PdsNode):
            raise TypeError("New child must be a PdsNode instance.")

        # Default indent for a new child in this block
        new_child_node.indent_level = self.indent_level + 4 # Assuming standard 4-space indent

        if before_node_instance:
            try:
                idx = self.children.index(before_node_instance)
                self.children.insert(idx, new_child_node)
                return True
            except ValueError: pass # Fall through if before_node_instance not found

        if after_node_instance:
            try:
                idx = self.children.index(after_node_instance)
                self.children.insert(idx + 1, new_child_node)
                return True
            except ValueError: pass # Fall through

        # Default: add before trailing comments/blank lines, otherwise at the end.
        insert_idx = len(self.children)
        for i in reversed(range(len(self.children))):
            if not isinstance(self.children[i], (PdsComment, PdsBlankLine)):
                insert_idx = i + 1
                break
            # If we iterate through all and they are all comments/blanks, insert at beginning of them or end.
            # Sensible default is usually end of non-comment/blank content.
            # If loop finishes and i is 0 and it was a comment/blank, insert_idx remains len(children) (append)
            # If loop finishes and i is -1 (empty list), insert_idx remains 0 (insert at start)

        self.children.insert(insert_idx, new_child_node)
        return True


    def get_structural_components(self, shallow_block=False):
        # structural components for the block itself: class, key (with type), line comment
        key_comp = (type(self.key).__name__, self.key) if self.key is not None else (type(None).__name__, None)

        if shallow_block: # Only compare block header
            return (self.__class__.__name__, key_comp, self.comment_text_on_line)
        else: # Deep comparison including children
            # Convert children to their structural components recursively
            children_comps = tuple(c.get_structural_components(shallow_block=False) for c in self.children)
            return (self.__class__.__name__, key_comp, self.comment_text_on_line, children_comps)


class PdsParser:
    def __init__(self):
        self.tokens = []
        self.current_token_index = 0
        self.root_nodes = [] # List of PdsNode at the top level of the file
        self.parent_stack = [] # Stack of PdsBlock/PdsList nodes during parsing for context

    def _error(self, message, token_override=None):
        token_info = ""
        token_to_report = token_override if token_override else self._peek()
        
        if token_to_report and token_to_report.type != 'EOF':
            token_info = f"at token '{token_to_report.value}' (type: {token_to_report.type}) on line {token_to_report.line}, col {token_to_report.column}"
        elif self.current_token_index > 0 and self.current_token_index <= len(self.tokens): # current_token_index can be len(tokens) if at EOF
            prev_token = self.tokens[self.current_token_index -1]
            token_info = f"after token '{prev_token.value}' (type: {prev_token.type}) on line {prev_token.line}, col {prev_token.column}"
        else: # Very start of file or empty token list
            token_info = "at beginning/empty file"
        raise ValueError(f"Parser Error: {message}. {token_info}")


    def _peek(self, offset=0):
        idx = self.current_token_index + offset
        if idx < len(self.tokens):
            return self.tokens[idx]
        # This part handles peeking beyond the end.
        if self.tokens and self.tokens[-1].type == 'EOF':
            return self.tokens[-1]
        # If tokens is empty *before* any parsing or EOF isn't added, this might fail.
        # Handled by parse_file returning early if lexer fails/no tokens.
        # If we reach here, it means tokens *was* empty after a failed lex, but parsing started? Unlikely.
        # Add a safeguard if tokens list is unexpectedly empty
        if not self.tokens:
            # This indicates a severe issue - parser shouldn't be running on an empty token list
            # that doesn't even have an EOF placeholder.
            sys.stderr.write("Parser internal error: _peek called on empty token list.")
            # Create a dummy EOF to prevent subsequent errors, though the parse is already failed.
            dummy_token = PdsToken('EOF', '', 0, 0)
            self.tokens.append(dummy_token)
            return dummy_token

        raise IndexError(f"Parser internal error: Attempted to peek beyond EOF at index {idx} (total tokens: {len(self.tokens)}).")


    def _advance(self):
        if self.current_token_index < len(self.tokens) - 1: # Stop advancing at EOF
            self.current_token_index += 1
        return self.tokens[self.current_token_index -1] # Return the token just consumed

    def _consume(self, *expected_types):
        token = self._peek()
        if token.type in expected_types:
            return self._advance()
        self._error(f"Expected one of {expected_types} but got {token.type}", token_override=token)

    def _is_next(self, *token_types, offset=0):
        """
        Checks if the token at the given offset matches any of the expected types.
        """
        return self._peek(offset).type in token_types

    def is_eof(self):
        return self._peek().type == 'EOF'

    # --- Parsing Helper Methods ---
    def _parse_comment_node(self):
        token = self._consume('COMMENT')
        return PdsComment(token.value, token.line) # Lexer stores comment text without '#'

    def _parse_primitive_value(self, token_obj=None): # Pass token_obj, don't advance here.
        if token_obj is None: token_obj = self._peek()

        val_to_parse = token_obj.value
        type_of_val = token_obj.type

        parsed_value = None
        if type_of_val == 'IDENTIFIER':
            # Check for boolean literals "yes" and "no" (case-insensitive)
            if val_to_parse.lower() == "yes": parsed_value = True
            elif val_to_parse.lower() == "no": parsed_value = False
            # Otherwise, treat it as a string identifier
            else: parsed_value = val_to_parse
        elif type_of_val == 'NUMBER':
            # Attempt to parse as int or float
            try: parsed_value = float(val_to_parse) if '.' in val_to_parse else int(val_to_parse)
            # If conversion fails (e.g., malformed number string), keep it as a string
            except ValueError: parsed_value = val_to_parse
        elif type_of_val == 'STRING':
            # Remove outer quotes and unescape internal quotes/slashes
            val_str_quoted = token_obj.value # Value still includes quotes from lexer
            if len(val_str_quoted) >= 2 and val_str_quoted[0] == '"' and val_str_quoted[-1] == '"':
                val_str_inner = val_str_quoted[1:-1]
                # Unescape \" to " and \\ to \
                # Use re.sub with a function for clarity on replacements
                parsed_value = re.sub(r'\\(.)', lambda m: {'\\"': '"', '\\\\': '\\'}.get(m.group(0), m.group(1)), val_str_inner)
            else:
                 # Should not happen if lexer worked correctly, but fallback to raw value
                 parsed_value = val_to_parse

        # Only return parsed_value if it was successfully determined from a recognized type
        # If it's a different type (like LBRACE, RBRACE, EQUALS, OPERATOR, COMMENT, NEWLINE, EOF),
        # this function returns None, and the caller (_parse_statement or _parse_block_or_list_after_equals)
        # should handle it appropriately.
        if parsed_value is not None:
            return parsed_value

        # If the token type wasn't one of the primitive value types, return None.
        # The caller is responsible for consuming the token if a non-None value is returned.
        return None


    def _parse_value_for_kvp_or_op(self):
        # This function is called AFTER '=' or OPERATOR is consumed.
        # It should parse and consume the token(s) that form the value on the RHS.

        if self._is_next('LBRACE'):
            # The value is a Block or a List. Call the specific parser method.
            # Pass key_str_for_node=None and line_of_key as the context doesn't provide a key *for the value itself*,
            # but the LBRACE line is relevant for comments. is_rhs_of_equals=True.
            return self._parse_block_or_list_after_equals(key_str_for_node=None, line_of_key=self._peek().line, is_rhs_of_equals=True)
        else:
            # Attempt to parse a primitive value (IDENTIFIER, NUMBER, STRING)
            token = self._peek()
            parsed_value = self._parse_primitive_value(token)
            if parsed_value is not None:
                # If a primitive value was successfully parsed, consume the token.
                self._advance()
                return parsed_value
            else:
                # If it's not LBRACE and not a parseable primitive, it's an error.
                self._error(f"Expected a value (string, number, identifier, boolean) or an anonymous block/list {{...}} as RHS value", token_override=token)

        # Should not reach here, _error raises exception.
        return None


    def _parse_statement(self):
        # Parses a single top-level statement (KVP, OpCond, Block) or an item within a list (primitive, anonymous Block/List).
        # This method should be called only when the parser is positioned at the START of a potential statement/item.
        # Leading comments/blank lines should ideally be handled by the calling loop (_parse_file, _parse_block_content, _parse_block_or_list_after_equals for lists)
        # before calling this.

        start_token = self._peek()
        start_token_line = start_token.line

        # If the first token is an LBRACE, it's an anonymous block/list (depending on parent context, handled by the calling loop's logic)
        # or a named block without '=', handled below.
        # If the caller is a list parser, it knows to expect bare values or anonymous blocks/lists.
        # If the caller is the root or a block parser, it expects KVP, OpCond, named Block/List.

        # Check if the first token is a potential key or value start.
        if not (self._is_next('IDENTIFIER') or self._is_next('NUMBER') or self._is_next('STRING') or self._is_next('LBRACE')):
             # If we're here, and it's not EOF, COMMENT, NEWLINE, or RBRACE (handled by callers), it's unexpected.
             # This check is primarily a safeguard; callers should handle expected tokens like RBRACE/EOF.
             if not (self._is_next('EOF') or self._is_next('COMMENT') or self._is_next('NEWLINE') or self._is_next('RBRACE')):
                 self._error("Statement must start with IDENTIFIER, NUMBER, STRING, or LBRACE", token_override=start_token)
             # If it's one of the ignored types (EOF, COMMENT, NEWLINE), return None to signal caller to skip/exit.
             # If it's RBRACE, caller loop condition should handle it.
             return None


        # Look ahead to determine the structure:
        # IDENTIFIER/NUMBER/STRING + EQUALS  -> KVP (primitive value, block value, or list value)
        # IDENTIFIER/NUMBER/STRING + OPERATOR -> Operator Condition
        # IDENTIFIER/NUMBER/STRING + LBRACE -> Named Block/List (no equals)
        # LBRACE -> Anonymous Block/List

        if self._is_next('LBRACE'): # Anonymous block or list item
            # This case is complex: '{ effect = {} }' is an anonymous block.
            # '{ { item1 } { item2 } }' is a list of anonymous blocks.
            # The distinction is made in _parse_block_or_list_after_equals when called *after* `=`.
            # When called *here* (not after `=`), it's always a Block node (named if key, anonymous if no key).
            # So, if _parse_statement sees LBRACE first, it's an anonymous block node.
             # Pass key_for_block=None, line_of_key is the line of the LBRACE. is_rhs_of_equals=False.
            return self._parse_block_content(key_for_block=None, line_of_key=start_token_line, is_rhs_of_equals=False)


        # Assume it starts with IDENTIFIER, NUMBER, or STRING
        key_candidate_token = self._peek() # Peek the first token
        key_line = key_candidate_token.line
        key_raw_value = key_candidate_token.value # Value as read by lexer

        # Peek the second token to determine structure
        next_token = self._peek(1)

        if next_token.type == 'EQUALS': # Found pattern: key = ...
            key_token = self._consume(key_candidate_token.type) # Consume the key token
            self._consume('EQUALS') # Consume '=' token

            # The value can be a primitive, block, or list. _parse_value_for_kvp_or_op handles this.
            value_node_content = self._parse_value_for_kvp_or_op() # This function consumes the value token(s)

            # Look for comment on the same line immediately after the value.
            comment_on_line = None
            # The token *before* current_token_index is the last token of the value (primitive or '}'/'end of node').
            # Need to check if the next token is a COMMENT *and* on the same line as the *last token of the value*.
            # Let's find the line of the token *just consumed* before checking for comment.
            last_consumed_token_line = self.tokens[self.current_token_index -1].line if self.current_token_index > 0 else key_line # Fallback to key_line

            if self._is_next('COMMENT') and self._peek().line == last_consumed_token_line:
                 comment_on_line = self._consume('COMMENT').value

            # Convert string key if needed (remove quotes, unescape)
            key_for_node = key_raw_value
            if key_candidate_token.type == 'STRING':
                # Remove outer quotes and unescape internal quotes/slashes for the key string.
                 key_for_node = key_raw_value[1:-1]
                 key_for_node = re.sub(r'\\(.)', lambda m: {'\\"': '"', '\\\\': '\\'}.get(m.group(0), m.group(1)), key_for_node)
            # Numbers parsed as keys should remain their numerical type? PDS treats number keys as strings often.
            # Let's keep it as is for now based on lexer NUMBER type. PdsNode.to_string stringifies keys.


            # value_node_content is already the parsed Node (Block/List) or primitive.
            return PdsKeyValuePair(key_for_node, value_node_content, key_line, comment_on_line)

        elif next_token.type == 'OPERATOR': # Found pattern: key OPERATOR ...
            key_token = self._consume(key_candidate_token.type) # Consume key token
            op_token = self._consume('OPERATOR') # Consume operator token

            # The value can be a primitive, block, or list. _parse_value_for_kvp_or_op handles this.
            value_node_content = self._parse_value_for_kvp_or_op() # This function consumes the value token(s)

            # Look for comment on the same line immediately after the value.
            comment_on_line = None
            last_consumed_token_line = self.tokens[self.current_token_index -1].line if self.current_token_index > 0 else key_line # Fallback to key_line
            if self._is_next('COMMENT') and self._peek().line == last_consumed_token_line:
                 comment_on_line = self._consume('COMMENT').value

            key_for_node = key_raw_value # Assuming operator keys are identifiers/numbers

            return PdsOperatorCondition(key_for_node, op_token.value, value_node_content, key_line, comment_on_line)

        elif next_token.type == 'LBRACE': # Found pattern: key { ... } (Named Block/List, NO equals)
             # This specific pattern `identifier { ... }` is often used for named blocks in PDS (e.g. `effect = { ... }` is KVP, but `modifier { ... }` is Block).
             # Let's assume this pattern *always* means a PdsBlock node.
            key_token = self._consume(key_candidate_token.type) # Consume key token
            # The LBRACE and block content are parsed by _parse_block_content.
            key_for_node = key_raw_value # Assuming block keys are identifiers/numbers

            # Pass is_rhs_of_equals=False as this pattern is not a value on the RHS of '='
            return self._parse_block_content(key_for_node, key_line, is_rhs_of_equals=False)

        else: # Bare Primitive Value (e.g., inside a List: { val1 "val 2" val3 })
            # If none of the structured patterns match, assume it's a bare primitive value.
            # This is valid when parsing items within a PdsList.
            # The calling loop (_parse_block_or_list_after_equals for lists) expects this.
            # If this happens at the root or inside a regular block, it's likely an error,
            # but we parse it here anyway and the caller (if expecting structured nodes) might handle it.

            # We already peeked key_candidate_token. Now consume it and parse its value.
            primitive_val_token = self._consume(key_candidate_token.type) # Consume IDENTIFIER, NUMBER, or STRING
            primitive_val = self._parse_primitive_value(primitive_val_token)

            # Primitive values parsed as list items typically don't have line comments attached to the value itself,
            # but comments can appear *after* them in the list stream. The list parsing loop handles those.
            comment_on_line = None # Primitive nodes don't store line comments in the current AST design.

            # Check if a primitive value was successfully parsed. It should be if the token type was right.
            if primitive_val is not None:
                return primitive_val
            else:
                # This case should not be reached if the initial check for IDENTIFIER/NUMBER/STRING passed.
                 self._error(f"Parser failed to parse primitive value from token '{primitive_val_token.value}' (type: {primitive_val_token.type})")

        # Should not reach here.
        return None


    def _parse_block_or_list_after_equals(self, key_str_for_node, line_of_key, is_rhs_of_equals=True):
        # This method is called *after* the '=' token has been consumed for a KVP or OpCond.
        # It expects the next token to be '{'.
        lbrace_token = self._consume('LBRACE')

        comment_on_lbrace_line = None
        # Check for comment on the line of the opening brace '{'.
        # The LBRACE token was just consumed. Peek the token BEFORE it (which was '=') to get the line.
        # If there was no '=' (e.g. anonymous block after '='), use the key_line passed in.
        line_to_check_for_comment = self.tokens[self.current_token_index -2].line if self.current_token_index > 1 and self.tokens[self.current_token_index -2].type == 'EQUALS' else lbrace_token.line # Check line of '=' or '{'

        if self._is_next('COMMENT') and self._peek().line == line_to_check_for_comment:
             comment_on_lbrace_line = self._consume('COMMENT').value


        # Heuristic to distinguish list from block:
        # Look for the first *significant* token (not NEWLINE or COMMENT) inside the braces.
        temp_idx = self.current_token_index
        while temp_idx < len(self.tokens):
            token_at_temp = self.tokens[temp_idx]
            if token_at_temp.type in ('NEWLINE', 'COMMENT'):
                temp_idx += 1
            else:
                break

        # If the block is empty (next significant token is RBRACE), it's a PdsBlock by convention.
        if self._is_next('RBRACE', offset=temp_idx - self.current_token_index):
             # Parse as an empty block
             # Pass the key and line associated with the KVP/OpCond that contained this value
             return self._parse_block_content(key_str_for_node, line_of_key, block_opening_comment=comment_on_lbrace_line, is_rhs_of_equals=True)


        is_likely_block = False
        if temp_idx < len(self.tokens):
            first_significant_token_in_braces = self.tokens[temp_idx]

            if first_significant_token_in_braces.type in ('IDENTIFIER', 'STRING', 'NUMBER'):
                # If the first significant item starts with a potential key (Identifier/String/Number)
                # AND is followed by '=', OPERATOR, or '{' (for a named sub-block), it's likely a Block.
                if temp_idx + 1 < len(self.tokens):
                    next_token_after_first = self.tokens[temp_idx+1]
                    if next_token_after_first.type in ('EQUALS', 'OPERATOR', 'LBRACE'):
                        is_likely_block = True
            elif first_significant_token_in_braces.type == 'LBRACE':
                # If the first *significant* token inside is LBRACE, it's an anonymous block item.
                # A collection of anonymous blocks `{ {item1} {item2} }` is typically parsed as a List.
                is_likely_block = False # Starting with an anonymous block means it's a list of blocks.
            # If first significant token is RBRACE (handled above) or something unexpected, it's not a block header.

        if is_likely_block:
             # If the heuristic says it's likely a block
             # Pass the key and line associated with the KVP/OpCond that contained this value
            return self._parse_block_content(key_str_for_node, line_of_key, block_opening_comment=comment_on_lbrace_line, is_rhs_of_equals=True)
        else: # Parse as PdsList
            # Pass the key and line associated with the KVP/OpCond that contained this value
            list_node = PdsList(key_str_for_node, [], line_of_key, comment_on_lbrace_line)

            # Add the list node to the parent stack for context, even though it's a value node.
            # This helps child nodes determine their indent level relative to this list.
            # The "real" parent context (the KVP/OpCond containing this list) is already on the stack's level below.
            self.parent_stack.append(list_node)

            while not self._is_next('RBRACE') and not self.is_eof():
                # Consume any `NEWLINE` or `COMMENT` tokens between list items.
                self._skip_newlines_and_comments_in_list_content()
                if self._is_next('RBRACE') or self.is_eof(): break # Check again after skipping

                # Parse a list item. This can be a primitive value or an anonymous Block/List/KVP/OpCond node.
                list_item = self._parse_statement()

                if list_item is not None:
                    # If the list item is a PdsNode (e.g., anonymous block/list), set its indent level.
                    # The indent level for items *within* a multi-line list should be the indent of the list node itself + 4.
                    # The list node's indent_level is set by its parent (the KVP/OpCond containing it).
                    # However, setting the indent here means PdsList.to_string will use this level when rendering the child.
                    # For multi-line lists, children are indented relative to the list.
                    # Let's ensure the indent level is set. If parent_stack has the list node, use its indent.
                    # The list node itself should get its indent from the parent *before* it was pushed onto the stack.
                    # Let's assume the list_node's indent_level is correctly set later by the KVP/OpCond parent.
                    # The child's indent should be list_node.indent_level + 4.
                    if isinstance(list_item, PdsNode):
                         # Set indent relative to the list node itself.
                         # The list_node's indent will be set by its containing KVP/Block.
                         # For now, rely on PdsNode.to_string using its own indent_level.
                         # A better way might be to set list_item.indent_level = list_node.indent_level + 4 here.
                         # But this is slightly circular. Let's trust to_string indent logic for now.
                         pass # Indent level is handled by the node's to_string based on the indent passed *into* it.
                              # Which means list_node's to_string must pass list_node.indent_level + 4.
                              # And list_node's indent_level must be set by the KVP/OpCond parent.

                    list_node.values.append(list_item)
                elif not self._is_next('RBRACE') and not self.is_eof():
                    # If _parse_statement returned None unexpectedly
                     self._error("Parser stuck in list content before token", token_override=self._peek())

            self._consume('RBRACE') # Consume the closing brace of the list
            self.parent_stack.pop() # Pop the list node from the stack
            return list_node

    def _skip_newlines_and_comments_in_list_content(self):
        # Consume any `NEWLINE` or `COMMENT` tokens until a significant token or RBRACE/EOF is found.
        # These are treated as ignored whitespace/comments *within* a list.
        while self._is_next('NEWLINE') or self._is_next('COMMENT'):
            self._advance()
            if self.is_eof(): break

    def _parse_block_content(self, key_for_block, line_of_key, block_opening_comment=None, is_rhs_of_equals=False):
        # This method is called after the key (if any) has been parsed.
        # If it's not the RHS of an equals (e.g. `modifier { ... }`), we need to consume the LBRACE.
        # If it *is* the RHS of an equals (e.g. `key = { ... }`), the LBRACE was consumed by _parse_block_or_list_after_equals.
        if not is_rhs_of_equals:
            lbrace_token = self._consume('LBRACE')
            # Check for comment on the line of the opening brace '{'.
            if self._is_next('COMMENT') and self._peek().line == lbrace_token.line:
                 block_opening_comment = self._consume('COMMENT').value
        # Note: If is_rhs_of_equals is True, block_opening_comment is passed in by the caller.

        block_node = PdsBlock(key_for_block, line_of_key, block_opening_comment)

        # Determine the indent level for this block node itself.
        # If parent_stack is empty, it's a top-level block (indent 0).
        # Otherwise, it's a child block (parent indent + 4).
        current_parent_node = self.parent_stack[-1] if self.parent_stack else None
        block_node.indent_level = (current_parent_node.indent_level + 4) if current_parent_node else 0


        self.parent_stack.append(block_node) # Push the block node onto the stack

        # Track the line number of the last consumed token/node for blank line detection.
        # If we consumed LBRACE here, use its line. If LBRACE was consumed by caller, use the line passed in (likely key line).
        # A better approach is the line of the token *after* the LBRACE/comment sequence that started the content.
        last_parsed_line = block_node.line_number # Start with the block's own line

        while not self._is_next('RBRACE') and not self.is_eof():
            current_token_line = self._peek().line

            # Handle blank lines by comparing current token's line with last parsed node's line
            # Blank lines are added as explicit nodes in blocks.
            if current_token_line > last_parsed_line + 1:
                for l_num in range(last_parsed_line + 1, current_token_line):
                    blank_line_node = PdsBlankLine(l_num)
                    # Blank lines should be indented at the same level as siblings
                    blank_line_node.indent_level = block_node.indent_level + 4
                    block_node.add_child(blank_line_node)

            # Consume all `NEWLINE` tokens that follow directly (e.g., in a sequence of blank lines)
            while self._is_next('NEWLINE'):
                last_parsed_line = self._advance().line # Update last_parsed_line

            if self._is_next('RBRACE') or self.is_eof(): break # Check again after skipping

            if self._is_next('COMMENT'): # Comments are explicit nodes within blocks
                comment_node = self._parse_comment_node()
                # Indent level for child comment is relative to parent block
                comment_node.indent_level = block_node.indent_level + 4
                block_node.add_child(comment_node)
                last_parsed_line = comment_node.line_number # Update last_parsed_line
                continue # Continue the loop to look for the next statement

            # Parse an actual statement (KVP, OpCond, nested Block, or List)
            # _parse_statement will handle consuming the tokens for the child node
            child_node = self._parse_statement()

            if child_node:
                 # Ensure the parsed item is a Node type (primitives are not allowed directly in blocks)
                 if not isinstance(child_node, PdsNode):
                     self._error(f"Block '{key_for_block if key_for_block is not None else 'Anonymous'}' cannot directly contain bare primitive value '{child_node!r}'. KVP, OpCond, Block, List or Comment expected.", token_override=self.tokens[self.current_token_index - (1 if self.current_token_index > 0 else 0)])

                 # Indent level for child node is relative to parent block
                 child_node.indent_level = block_node.indent_level + 4
                 block_node.add_child(child_node)
                 # Update last_parsed_line to the line of the last token consumed for the child node
                 last_parsed_line = self.tokens[self.current_token_index - 1].line
            elif not self._is_next('RBRACE') and not self.is_eof():
                 # If _parse_statement returned None but we aren't at the end of the block/file
                 # This could happen if there's an unexpected token that wasn't handled as an error.
                 self._error(f"Parser stuck in block '{key_for_block if key_for_block is not None else 'Anonymous'}' before token", token_override=self._peek())

        self._consume('RBRACE') # Consume the closing brace of the block
        self.parent_stack.pop() # Pop the block node from the stack
        return block_node

    def parse_file(self, filepath):
        self.root_nodes = []
        self.parent_stack = []
        self.current_token_index = 0
        self.tokens = [] # Reset tokens for each new parse operation

        if not os.path.exists(filepath):
            sys.stderr.write(f"Error: File not found: {filepath}\n")
            return []

        try:
            with open(filepath, 'r', encoding='utf-8-sig') as f:
                text_content = f.read()
        except UnicodeDecodeError:
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    text_content = f.read()
            except Exception as e_inner:
                sys.stderr.write(f"Error reading {filepath} (UTF-8 fallback failed): {e_inner}\n")
                return []
        except Exception as e:
            sys.stderr.write(f"Error reading {filepath}: {e}\n")
            return []

        lexer = PdsLexer(text_content)
        try:
            self.tokens = lexer.tokenize()
        except ValueError as lex_err:
            sys.stderr.write(f"Lexer error in {filepath}: {lex_err}\n")
            return [] # Returns empty list of nodes if lexer failed

        # Handle truly empty files or files containing only BOM/whitespace/comments after lexing
        # The lexer might produce an empty token list or just an EOF token.
        # Ensure there's at least an EOF token if the list isn't empty but doesn't end with EOF.
        if self.tokens and self.tokens[-1].type != 'EOF':
             last_token = self.tokens[-1]
             self.tokens.append(PdsToken('EOF', '', last_token.line, last_token.column + len(last_token.value)))
        # If tokens is empty *before* adding EOF, add a dummy one
        elif not self.tokens:
             self.tokens.append(PdsToken('EOF', '', 1, 1))


        # --- Main parsing loop for top-level statements ---
        last_parsed_line = 0 # Track line of last parsed *node* for blank line detection at root

        # The loop condition `while not self.is_eof():` is now safe because `self.tokens`
        # is guaranteed to have at least an EOF token at this point (or it returned earlier).
        while not self.is_eof():
            current_token = self._peek()
            current_token_line = current_token.line

            # Handle blank lines before the next statement
            # Compare line of current token with the line of the last token of the previously parsed node.
            if current_token_line > last_parsed_line + 1:
                for l_num in range(last_parsed_line + 1, current_token_line):
                    # Root level blank lines have indent 0.
                    self.root_nodes.append(PdsBlankLine(l_num))

            # Consume all `NEWLINE` tokens that follow directly (e.g., in a sequence of blank lines)
            # This should ideally be within the blank line handling or the main loop's skips,
            # not a separate block like this UNLESS we want to consume *all* following newlines.
            # Let's rely on the blank line check and the skip logic within _parse_statement callers.
            # This explicit newline consumption might be redundant or harmful if it consumes newlines
            # that should separate list items etc. Let's remove it from the main loop.
            # while self._is_next('NEWLINE'):
            #     last_parsed_line = self._advance().line # This doesn't update last_parsed_line correctly if node wasn't added.

            # --- Re-evaluate skipping logic in main loop ---
            # We need to skip comments and blank lines *before* trying to parse a statement.
            # The logic for blank lines above adds nodes, but doesn't consume the NEWLINE tokens.
            # Let's add robust skipping here.
            while True:
                current_token = self._peek()
                current_token_line = current_token.line

                # Add blank lines if gap exists
                if current_token_line > last_parsed_line + 1:
                     for l_num in range(last_parsed_line + 1, current_token_line):
                          self.root_nodes.append(PdsBlankLine(l_num))
                     # Update last_parsed_line after adding blank line nodes
                     last_parsed_line = current_token_line - 1 # The blank lines end on the line before current_token_line


                # Consume comments and actual NEWLINE tokens
                if self._is_next('COMMENT'):
                     comment_node = self._parse_comment_node()
                     comment_node.indent_level = 0 # Root comments have 0 indent
                     self.root_nodes.append(comment_node)
                     last_parsed_line = comment_node.line_number # Update last_parsed_line
                     continue # Check for more skips/blank lines before parsing statement

                if self._is_next('NEWLINE'):
                    last_parsed_line = self._advance().line # Consume newline, update line number
                    continue # Check for more skips/blank lines / new blank lines

                # If it's not COMMENT or NEWLINE and not EOF, it must be the start of a statement or RBRACE (error at root)
                break # Exit skip loop if we found something else or EOF

            # After skipping, check if we are at EOF
            if self.is_eof(): break

            # If we are here, the current token should be the start of a statement.
            # We are at the correct position to call _parse_statement.
            try:
                # Root statements are not values of anything else, so is_value=False is default for to_string
                node = self._parse_statement()
                if node:
                    # _parse_statement should return a PdsNode for root/block content, or primitive for list items.
                    # At root, we only accept PdsNode.
                    if not isinstance(node, PdsNode):
                        self._error(f"Root level cannot contain bare primitive value: {node!r}", token_override=self.tokens[self.current_token_index - 1] if self.current_token_index > 0 else current_token)

                    node.indent_level = 0 # Ensure root nodes have 0 indent
                    self.root_nodes.append(node)

                    # Update last_parsed_line to the line of the last token consumed by _parse_statement
                    # _parse_statement consumes all tokens for the statement. The index points *after* the last token.
                    if self.current_token_index > 0:
                        last_parsed_line = self.tokens[self.current_token_index - 1].line
                    else:
                        # This case should not happen if _parse_statement successfully parsed and consumed.
                        # Fallback: use the current token line before statement parsing attempt.
                         last_parsed_line = current_token_line # Use the line where we *expected* a statement to start.

                # If _parse_statement returned None (e.g. only comments/newlines were there, handled by skip loop), loop continues.
                # If it's not EOF and _parse_statement didn't consume tokens but didn't error, it's stuck.
                # The skip loop and _parse_statement's internal error handling should prevent getting stuck.
                # This check is likely redundant now.
                # elif not self.is_eof():
                #      self._error(f"Parser did not produce a node and is not at EOF.")

            except ValueError as parse_err:
                sys.stderr.write(f"Parser error in {filepath}: {parse_err}\n")
                # Stop parsing on the first critical error. Return the nodes parsed so far.
                return self.root_nodes
            except Exception as e:
                 sys.stderr.write(f"Unexpected error during parsing {filepath}: {e}\n")
                 # Log unexpected errors and stop.
                 return self.root_nodes


        # Final check for blank lines at the end of the file
        # The `last_parsed_line` is the line of the last token of the last *content* node.
        # The current token index is at EOF. Get the EOF token's line.
        eof_token = self._peek()
        eof_line = eof_token.line

        # Add blank lines from the line *after* the last parsed node's last token up to the EOF line - 1.
        if eof_line > last_parsed_line + 1:
             for l_num in range(last_parsed_line + 1, eof_line):
                 self.root_nodes.append(PdsBlankLine(l_num))
        # If the file ends with a newline immediately after the last token, EOF is on the next line,
        # so eof_line = last_parsed_line + 1. No blank lines are added in this case, which is correct.


        return self.root_nodes

    @staticmethod
    def _nodes_to_string(nodes_list):
        output = []
        for node in nodes_list:
            # Root nodes are not values of anything else.
            # Use node's stored indent level if available, default to 0 for safety.
            node_indent = getattr(node, 'indent_level', 0)
            if node_indent is None: node_indent = 0
            # Call to_string with is_value=False for root nodes
            output.append(node.to_string(current_indent=node_indent, is_value=False))
        return "".join(output)


    def to_string(self): # Instance method to print parsed content
        return PdsParser._nodes_to_string(self.root_nodes)