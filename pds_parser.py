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
                        val_to_store = token_value_raw[1:] # Store comment text without '#'
                    
                    self._add_token(token_type, val_to_store, current_char_start_line, current_char_start_column)
                    
                    for _ in range(len(token_value_raw)): # Advance lexer position over the matched token
                        self._advance_char()
                    matched_pattern = True
                    break
            
            if not matched_pattern:
                # This is a lexer error, it should probably raise an exception or log an error token
                # For now, let's skip the character to avoid infinite loops on unknown chars
                # but this is not ideal for robust parsing.
                # Error should be: raise ValueError(...)
                # In provided code, it was already raising ValueError.
                raise ValueError(f"Lexer Error: Unexpected character '{char}' at line {self.line}, column {self.column}")

        self._add_token('EOF', '', self.line, self.column) # Add EOF token at the end
        return self.tokens


class PdsNode:
    def __init__(self, line_number=-1):
        self.line_number = line_number
        self.indent_level = 0
        self.comment_text_on_line = None # For comments on the same line as the node's definition

    def to_string(self, current_indent=0, is_inline_context=False):
        raise NotImplementedError(f"to_string() not implemented for {self.__class__.__name__}")

    def __repr__(self):
        key_repr = getattr(self, 'key', 'N/A')
        comment_repr = self.comment_text_on_line
        if not isinstance(key_repr, str): key_repr = str(key_repr) # Handle non-string keys like numbers
        comment_str = f", Comment='{comment_repr[:10]}...'" if comment_repr else ""
        
        cl_name = self.__class__.__name__
        spec_repr = ""
        if isinstance(self,PdsComment): spec_repr=f"'{getattr(self,'comment_text','')}[:20]...'"
        elif isinstance(self,PdsBlankLine): spec_repr="Blank"
        elif isinstance(self,PdsBlock) or (isinstance(self,PdsKeyValuePair) and isinstance(getattr(self,'value',None),PdsBlock)):
             spec_repr=f"{key_repr}={{...}}"
        else: spec_repr=f"K='{key_repr}'" # Fallback for KVP, List, OperatorCondition
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
            return self.get_structural_components(shallow_block=False) == \
                   other.get_structural_components(shallow_block=False)
        except NotImplementedError as e:
            # Log error or handle, but for robustness in comparison, treat as unequal if not implemented
            sys.stderr.write(f"ERROR: __eq__ called on class without get_structural_components: {e}\n")
            return False

    def __ne__(self, other):
        equal = self.__eq__(other)
        return False if equal is NotImplemented else not equal # Consistent with Python 3

    def __hash__(self):
        try:
            return hash(self.get_structural_components(shallow_block=False))
        except NotImplementedError:
            # Fallback hash if structural components aren't defined,
            # though this means it won't hash well with other identical-looking objects.
            return id(self) # Or hash((self.__class__.__name__, self.line_number)) if line_number is reliable

    def get_comparator_key(self, shallow_block_for_seq_matcher=False):
        # This is the key used by difflib.SequenceMatcher.
        # By default, it's the full structural components.
        # shallow_block can be True if we only want to match block headers, not their content, for SM.
        return self.get_structural_components(shallow_block=shallow_block_for_seq_matcher)

class PdsComment(PdsNode):
    def __init__(self, comment_text, line_number=-1):
        super().__init__(line_number=line_number)
        self.comment_text = comment_text # Does not include the '#'

    def to_string(self, current_indent=0, is_inline_context=False): # Comments ignore inline context
        indent_str = " " * current_indent
        return f"{indent_str}#{self.comment_text}\n" # Comments always end with a newline

    def copy(self):
        new_node = PdsComment(comment_text=self.comment_text, line_number=self.line_number)
        return self._base_copy_attrs(new_node)

    def get_structural_components(self, shallow_block=False): # shallow_block is irrelevant here
        return (self.__class__.__name__, self.comment_text)


class PdsBlankLine(PdsNode):
    def __init__(self, line_number=-1):
        super().__init__(line_number=line_number)

    def to_string(self, current_indent=0, is_inline_context=False): # Blank lines ignore context
        return "\n" # A blank line node represents one empty line

    def copy(self):
        new_node = PdsBlankLine(line_number=self.line_number)
        return self._base_copy_attrs(new_node)

    def get_structural_components(self, shallow_block=False): # shallow_block is irrelevant
        return (self.__class__.__name__,) # All blank lines are structurally identical for comparison

class PdsKeyValuePair(PdsNode):
    # Pattern for unquoted identifiers (used for intelligent quoting in to_string)
    # Needs to be robust: typically alphanumeric, underscore, dot, colon, at-sign, hyphen.
    # Cannot start or end with hyphen if it's part of a multi-part identifier like a-b-c,
    # but can contain hyphens. Simple identifiers can be just 'a'.
    _lexer_identifier_pattern = r'[\w\.:@\-]+(?:[\w\.:@\-]*[\w\.:@\-])?'


    def __init__(self, key, value, line_number=-1, comment_text_on_line=None):
        super().__init__(line_number=line_number)
        self.key = key # string or number (parsed from IDENTIFIER or NUMBER token)
        self.value = value # primitive, or PdsBlock (for key = { ...block_content... })
        self.comment_text_on_line = comment_text_on_line

    # CORRECTED SIGNATURE HERE:
    def to_string(self, current_indent=0, is_inline_context=False): # Renamed from is_inline_context_ignored
        indent_str = " " * current_indent
        
        # Key formatting (quoting if necessary, though less common for keys than values)
        # For simplicity, assuming keys are typically simple identifiers that don't need quotes.
        # If keys can be complex, this would need quoting logic similar to values.
        key_actual_str = str(self.key) # Default to string representation
        # Example more robust key quoting (if keys can be complex strings):
        # if isinstance(self.key, str) and (not re.fullmatch(PdsKeyValuePair._lexer_identifier_pattern, self.key) or any(c in self.key for c in ' \t#={}"')):
        #    key_actual_str = f'"{self.key.replace("\\", "\\\\").replace("\"", "\\\"")}"'

        line_content_start = f"{indent_str}{key_actual_str} = "
        value_actual_str = ""
        ends_with_newline = True # Most KVPs end with a newline unless value is multiline block

        if isinstance(self.value, PdsBlock):
            block_node = self.value
            # PdsBlock's to_string needs to know its context for potential inline rendering
            # (e.g. key = { child_key = 1 } vs key = {\n ... \n})
            # Pass current_indent for the block to base its children on, and is_inline_context=True
            # to suggest it *can* be inline if it's simple enough.
            # The 'is_inline_context' passed to block_node.to_string here IS IMPORTANT.
            block_render_str = block_node.to_string(current_indent, is_inline_context=True) # Use the passed is_inline_context if KVP itself is in one? No, KVP asks its value block to be inline.
            
            # Check if the block rendered itself inline (no newlines, starts/ends with braces)
            if block_render_str.strip().startswith("{") and block_render_str.strip().endswith("}") and "\n" not in block_render_str:
                value_actual_str = block_render_str.strip() # e.g. { child_key = 1 }
                # ends_with_newline remains true, KVP line itself will add it.
            else: # Multiline block value
                # The block's to_string should handle its own indentation starting from current_indent
                # and its own newlines. We just append its output.
                # The lstrip() is important if the block's to_string adds its own initial indent based on current_indent.
                value_actual_str = block_render_str.lstrip() 
                ends_with_newline = False # Block provides its own final newline
        elif isinstance(self.value, str):
            # Quoting logic for string values
            # Check if it's a simple identifier that doesn't require quotes
            is_simple_identifier = re.fullmatch(PdsKeyValuePair._lexer_identifier_pattern, self.value)
            # Keywords that are often unquoted even if they look like identifiers
            is_keyword_like = self.value.lower() in ["yes", "no", "rgb", "hsv", "hsv360"] # Add other common ones
            
            # Conditions that necessitate quoting:
            # - Not a simple identifier (contains spaces, special chars not in pattern, etc.)
            # - Contains problematic characters like space, tab, #, =, {, }, "
            # - Is an empty string
            needs_quoting = not is_simple_identifier or \
                            any(c in self.value for c in ' \t#={}"') or \
                            not self.value # Empty string must be quoted ""
            
            if is_keyword_like and is_simple_identifier: # yes, no, rgb, hsv are typically not quoted
                 value_actual_str = self.value
            elif self.key == "icon" and self.value.startswith("@") and is_simple_identifier: # Special case for icon = @foo
                 value_actual_str = self.value
            elif needs_quoting:
                # Escape backslashes and double quotes within the string
                escaped_value = self.value.replace('\\', '\\\\').replace('"', '\\"')
                value_actual_str = f'"{escaped_value}"'
            else: # It's a simple identifier that doesn't need quotes
                value_actual_str = self.value
        elif isinstance(self.value, bool):
            value_actual_str = "yes" if self.value else "no"
        else: # Numbers (int, float) or other types (should be rare)
            value_actual_str = str(self.value)
        
        line_content = line_content_start + value_actual_str
        if self.comment_text_on_line: 
            line_content += f" # {self.comment_text_on_line}"
        
        return line_content + ("\n" if ends_with_newline else "")

    def copy(self):
        # Primitives are copied by value. PdsNode instances need deep copy.
        new_value = self.value.copy() if isinstance(self.value, PdsNode) else self.value
        new_node = PdsKeyValuePair(self.key, new_value, self.line_number, self.comment_text_on_line)
        return self._base_copy_attrs(new_node)

    def get_structural_components(self, shallow_block=False):
        # If value is a PdsBlock, its structural components depend on 'shallow_block'
        value_comp = None
        if isinstance(self.value, PdsNode):
            value_comp = self.value.get_structural_components(shallow_block=shallow_block)
        else: # Primitive value
            value_comp = self.value
        return (self.__class__.__name__, self.key, value_comp, self.comment_text_on_line)

class PdsList(PdsNode): # Represents key = { val1 "val 2" ... }
    def __init__(self, key, values, line_number=-1, comment_text_on_line=None):
        super().__init__(line_number=line_number)
        self.key = key # string or number
        self.values = values # List of primitives (str, int, float, bool) or PdsNode (e.g. anonymous blocks)
        self.comment_text_on_line = comment_text_on_line

    def to_string(self, current_indent=0, is_inline_context=False): # is_inline_context is a hint from parent
        indent_str = " " * current_indent
        
        # Heuristic: if all values are simple (not PdsNode) and short, render inline
        all_simple_primitives = all(not isinstance(v, PdsNode) for v in self.values)
        # Estimate length of primitive values if rendered inline
        temp_value_str_for_len_check = " ".join([str(v) for v in self.values if not isinstance(v, PdsNode)])

        # Inline if: in inline context (e.g. part of a larger KVP), OR few simple items, OR short total length
        # Note: is_inline_context is more of a request; the list decides if it *can* be inline.
        # For PdsList, it's less about `is_inline_context` from parent and more about its own content.
        # Let's simplify: inline if few simple items or short.
        can_be_inline = all_simple_primitives and \
                        (len(self.values) == 0 or len(self.values) <= 3 or len(temp_value_str_for_len_check) < 50)

        if can_be_inline: # Inline format: key = { val1 val2 "val 3" }
            formatted_values = []
            for v_item in self.values: # Should be primitives here due to all_simple_primitives check
                if isinstance(v_item, str):
                    is_simple_id = re.fullmatch(PdsKeyValuePair._lexer_identifier_pattern, v_item)
                    # Unquoted 'yes'/'no' are common in lists, treat them as identifiers unless forced by content
                    is_bool_like_keyword = v_item.lower() in ["yes", "no"] 
                    
                    needs_q = not is_simple_id or \
                                any(c in v_item for c in ' \t#={}"') or \
                                not v_item # Empty string
                    
                    if is_bool_like_keyword and is_simple_id and not needs_q : # Render yes/no unquoted if they are simple
                        formatted_values.append(v_item)
                    elif needs_q:
                        formatted_values.append(f'"{v_item.replace("\\\\", "\\\\\\\\").replace("\"", "\\\"")}"')
                    else: # Simple identifier, not bool-like, doesn't need quotes
                        formatted_values.append(v_item)
                elif isinstance(v_item, bool):
                    formatted_values.append("yes" if v_item else "no")
                else: # Numbers
                    formatted_values.append(str(v_item))
            
            value_str = " ".join(formatted_values)
            inner_content = f" {value_str} " if value_str else " " # Ensure space if empty: key = { }
            base_string = f"{indent_str}{self.key} = {{{inner_content}}}"
            if self.comment_text_on_line:
                base_string += f" # {self.comment_text_on_line}"
            return base_string + "\n"
        else: # Multi-line format: key = {\n  val1\n  val2\n  { anon_block }\n}
            output_lines = []
            open_brace_line = f"{indent_str}{self.key} = {{"
            if self.comment_text_on_line:
                open_brace_line += f" # {self.comment_text_on_line}"
            output_lines.append(open_brace_line + "\n")

            child_render_indent = current_indent + 4 # Standard indent for children
            for value_item in self.values:
                if isinstance(value_item, PdsNode): # e.g. an anonymous block { ... }
                    value_item.indent_level = child_render_indent # Ensure indent before to_string
                    # Anonymous blocks in lists are usually not requested to be inline by the list itself.
                    output_lines.append(value_item.to_string(child_render_indent, is_inline_context=False))
                else: # Primitive value, render on its own indented line
                    val_str_item = "" # Copied from inline logic for primitives for consistency
                    if isinstance(value_item, str):
                        is_simple_id = re.fullmatch(PdsKeyValuePair._lexer_identifier_pattern, value_item)
                        is_bool_like_keyword = value_item.lower() in ["yes", "no"]
                        needs_q = not is_simple_id or any(c in value_item for c in ' \t#={}"') or not value_item
                        if is_bool_like_keyword and is_simple_id and not needs_q: val_str_item = value_item
                        elif needs_q: val_str_item = f'"{value_item.replace("\\\\", "\\\\\\\\").replace("\"", "\\\"")}"'
                        else: val_str_item = value_item
                    elif isinstance(value_item, bool): val_str_item = "yes" if value_item else "no"
                    else: val_str_item = str(value_item)
                    output_lines.append(f"{' ' * child_render_indent}{val_str_item}\n")
            
            output_lines.append(f"{indent_str}}}\n")
            return "".join(output_lines)

    def copy(self):
        new_values = [v.copy() if isinstance(v, PdsNode) else v for v in self.values]
        new_node = PdsList(self.key, new_values, self.line_number, self.comment_text_on_line)
        return self._base_copy_attrs(new_node)

    def get_structural_components(self, shallow_block=False): # shallow_block applies to PdsBlock items in list
        # Convert PdsNode values in the list to their structural components
        values_comp = []
        for v_item in self.values:
            if isinstance(v_item, PdsNode):
                values_comp.append(v_item.get_structural_components(shallow_block=shallow_block))
            else: # Primitive
                values_comp.append(v_item)
        # Tuple of values for hashability
        return (self.__class__.__name__, self.key, tuple(values_comp), self.comment_text_on_line)

class PdsOperatorCondition(PdsNode): # e.g. key > value, or trigger_value = { limit = { key > value } }
    def __init__(self, key, operator, value, line_number=-1, comment_text_on_line=None):
        super().__init__(line_number=line_number)
        self.key = key # string or number (LHS)
        self.operator = operator # string (e.g., ">", "<=", "==")
        self.value = value # primitive or PdsBlock (RHS)      
        self.comment_text_on_line = comment_text_on_line

    # CORRECTED SIGNATURE HERE:
    def to_string(self, current_indent=0, is_inline_context=False): # Added is_inline_context (though ignored)
        indent_str = " " * current_indent
        key_actual_str = str(self.key) # Assuming keys don't need complex quoting here

        value_actual_str = ""
        # RHS value formatting (similar to KVP's value formatting)
        if isinstance(self.value, PdsBlock): # e.g. limit = { key > { block_val } } is rare, usually key > primitive
            # Render block with 0 relative indent, as it's part of this line
            # If it's a simple block, it might render inline. Otherwise, it might be an issue.
            # PDS usually doesn't have complex blocks on RHS of operator.
            # Pass the is_inline_context hint to the block.
            value_actual_str = self.value.to_string(0, is_inline_context=True).strip()
        elif isinstance(self.value, str):
            is_simple_id = re.fullmatch(PdsKeyValuePair._lexer_identifier_pattern, self.value)
            is_keyword_like = self.value.lower() in ["yes", "no", "root", "prev", "owner"] # Common unquoted RHS identifiers
            needs_q = not is_simple_id or any(c in self.value for c in ' \t#={}"') or not self.value
            if is_keyword_like and is_simple_id and not needs_q: value_actual_str = self.value
            elif needs_q: value_actual_str = f'"{self.value.replace("\\\\", "\\\\\\\\").replace("\"", "\\\"")}"'
            else: value_actual_str = self.value
        elif isinstance(self.value, bool):
            value_actual_str = "yes" if self.value else "no"
        elif isinstance(self.value, PdsNode): # Should not happen if not PdsBlock, error or unhandled
             # Pass is_inline_context if this node itself can be inline.
             value_actual_str = self.value.to_string(0, is_inline_context=True).strip() # Fallback
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
        return (self.__class__.__name__, self.key, self.operator, value_comp, self.comment_text_on_line)
    
class PdsBlock(PdsNode): # Represents key = { ...children... } or anonymous { ...children... }
    def __init__(self, key, line_number=-1, comment_text_on_line=None): # Key can be None for anonymous blocks
        super().__init__(line_number=line_number)
        self.key = key # string, number, or None
        self.children = [] # List of PdsNode instances
        self.comment_text_on_line = comment_text_on_line # Comment on the opening brace line: foo = { # THIS

    def add_child(self, node):
        if not isinstance(node, PdsNode):
            raise TypeError(f"Can only add PdsNode instances as children to PdsBlock, got {type(node)}")
        # Indent level of child is typically set by parser or by to_string based on parent's indent
        self.children.append(node)

    def to_string(self, current_indent=0, is_inline_context=False):
        indent_str = " " * current_indent
        block_key_prefix = ""
        if self.key is not None: # Named block: key = { ... }
            # Assume key doesn't need complex quoting, similar to KVP key
            block_key_prefix = f"{str(self.key)} = "
        # Else: Anonymous block: { ... } (prefix is empty)

        # Heuristic for inline rendering: key = { child_key = val } (no newlines, single simple KVP child)
        # is_inline_context is a strong hint from parent (e.g. KVP value) that inline is preferred if possible.
        can_attempt_inline_render = is_inline_context and len(self.children) == 1 and \
                                   isinstance(self.children[0], PdsKeyValuePair) and \
                                   not isinstance(self.children[0].value, PdsBlock) and \
                                   not self.comment_text_on_line and \
                                   not self.children[0].comment_text_on_line and \
                                   len(str(self.children[0].key)) + len(str(self.children[0].value)) < 40 # Child KVP is short

        if can_attempt_inline_render:
            # Render child KVP with zero indent relative to its own content, then strip.
            # The child KVP's to_string should not add indent if current_indent is 0.
            # It also should not add a trailing newline if its value isn't a multiline block.
            child_kvp_node = self.children[0]
            # Forcing child KVP to not add its own indent and rely on this block for context
            child_str_compact = child_kvp_node.to_string(current_indent=0).strip() # e.g. "child_key = value"
            
            # Check if child_str_compact itself contains newlines (e.g. if KVP value was unexpectedly complex)
            if "\n" not in child_str_compact:
                # This is the true inline form for a KVP's value: e.g. outer_key = { inner_key = val }
                return f"{block_key_prefix}{{{child_str_compact}}}" # No newline if truly inline for KVP value context

        # Standard multi-line rendering
        output_parts = []
        open_brace_line = f"{indent_str}{block_key_prefix}{{"
        if self.comment_text_on_line:
            open_brace_line += f" # {self.comment_text_on_line}"
        output_parts.append(open_brace_line) # No \n yet

        if not self.children: # Empty block: key = {} or {}
            output_parts.append("}\n") # Add closing brace and newline
        else:
            output_parts.append("\n") # Newline after opening brace if there are children
            child_render_indent = current_indent + 4
            for child_node in self.children:
                child_node.indent_level = child_render_indent # Ensure child knows its indent
                # Children determine their own inline-ness, pass False for is_inline_context generally
                output_parts.append(child_node.to_string(child_render_indent, is_inline_context=False)) # Child.to_string provides its own ending \n
            output_parts.append(f"{indent_str}}}\n") # Indented closing brace with newline
        
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
            # Preserve indent level of the position, or let new_child_node decide (usually parent + 4)
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
            # If all are comments/blanks, insert_idx will become 0 (or stay len if list was empty)
            # If we iterate through all and they are all comments/blanks, insert at beginning of them or end.
            # Sensible default is usually end of non-comment/blank content.
            if i == 0 and isinstance(self.children[i], (PdsComment, PdsBlankLine)): # If loop finishes, all were comments/blanks
                 insert_idx = len(self.children) # Append at very end if all are ignorable like comments.

        self.children.insert(insert_idx, new_child_node)
        return True


    def get_structural_components(self, shallow_block=False):
        if shallow_block: # Only compare block header (key, type, line comment)
            return (self.__class__.__name__, self.key, self.comment_text_on_line)
        else: # Deep comparison including children
            # Convert children to their structural components
            children_comps = tuple(c.get_structural_components(shallow_block=False) for c in self.children)
            return (self.__class__.__name__, self.key, self.comment_text_on_line, children_comps)

class PdsParser:
    def __init__(self):
        self.tokens = []
        self.current_token_index = 0
        self.root_nodes = [] # List of PdsNode at the top level of the file
        self.parent_stack = [] # Stack of PdsBlock nodes during parsing for context

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
        # Should not happen if EOF is always last token. If it does, means asking beyond EOF.
        return self.tokens[-1] # Return EOF token if out of bounds

    def _advance(self):
        if self.current_token_index < len(self.tokens) - 1: # Stop advancing at EOF
            self.current_token_index += 1
        return self.tokens[self.current_token_index -1] # Return the token just consumed

    def _consume(self, *expected_types):
        token = self._peek()
        if token.type in expected_types:
            return self._advance()
        self._error(f"Expected one of {expected_types} but got {token.type}", token_override=token)

    def _is_next(self, *token_types):
        return self._peek().type in token_types

    def is_eof(self):
        return self._peek().type == 'EOF'

    # --- Parsing Helper Methods ---
    def _parse_comment_node(self):
        token = self._consume('COMMENT')
        return PdsComment(token.value, token.line) # Lexer stores comment text without '#'

    def _parse_primitive_value(self, token_val_if_consumed=None):
        token_obj = None
        if token_val_if_consumed is None: # Standard path: peek and advance
            token_obj = self._peek()
        
        # Determine value and type to parse
        val_to_parse = token_val_if_consumed if token_val_if_consumed is not None else token_obj.value
        type_of_val = 'IDENTIFIER' # Default assumption if pre-consumed and type not passed
        if token_obj: # If we peeked, use its type
            type_of_val = token_obj.type
        
        # Based on type, convert value
        parsed_value = None
        if type_of_val == 'IDENTIFIER':
            if token_obj: self._advance() # Consume if we peeked
            if val_to_parse.lower() == "yes": parsed_value = True
            elif val_to_parse.lower() == "no": parsed_value = False
            else: parsed_value = val_to_parse # Keep as string identifier
        elif type_of_val == 'NUMBER':
            if token_obj: self._advance()
            try: # Attempt to convert to int or float
                parsed_value = float(val_to_parse) if '.' in val_to_parse else int(val_to_parse)
            except ValueError: # Should not happen if lexer NUMBER pattern is good
                parsed_value = val_to_parse # Fallback: keep as string (error?)
        elif type_of_val == 'STRING':
            if token_obj: self._advance()
            # Remove quotes and unescape internal quotes/backslashes
            val_str = val_to_parse[1:-1] # Remove surrounding "
            parsed_value = val_str.replace('\\"', '"').replace('\\\\', '\\')
        
        return parsed_value # Returns None if type not one of above (e.g. LBRACE, OPERATOR)


    def _parse_value_for_kvp_or_op(self): # Parses RHS of KVP or OperatorCondition
        if self._is_next('LBRACE'):
            # This is an anonymous block as a value, e.g., trigger = { limit = { THIS_BLOCK } }
            # Or foo = { ... } which is handled by _parse_block_or_list_after_equals
            # This context is for RHS of operator, or potentially value of KVP if not list/block.
            # For KVP, usually _parse_block_or_list_after_equals is called first.
            # This path here implies an anonymous block not forming a list.
            # Example: some_trigger = { limit = { x > { anon_block_val } } }
            # The key (None) and line number for this anonymous block are from the LBRACE.
            return self._parse_block_content(key_for_block=None, line_of_key=self._peek().line)
        else:
            pv = self._parse_primitive_value() # Tries to parse IDENTIFIER, NUMBER, STRING
            if pv is not None:
                return pv
            self._error(f"Expected a primitive value or an anonymous block {{...}} as RHS value")
        return None # Should be unreachable due_to _error

    def _parse_statement_or_item(self):
        start_token_line = self._peek().line
        start_token_col = self._peek().column # For indent calc if needed, though indent is usually parent-based

        if self._is_next('COMMENT'):
            return self._parse_comment_node()
        
        if self._is_next('LBRACE'): # Anonymous block (can be item in a list, or RHS of OpCond)
            # This will consume LBRACE. Key is None.
            return self._parse_block_content(key_for_block=None, line_of_key=start_token_line)

        # Expect IDENTIFIER, NUMBER, or STRING for key/LHS or bare value
        if not (self._is_next('IDENTIFIER') or self._is_next('NUMBER') or self._is_next('STRING')):
             self._error("Statement or list item must start with IDENTIFIER, NUMBER, STRING, COMMENT, or LBRACE for anonymous block")

        # Consume what could be a key, or a bare primitive value (if in a list context)
        key_candidate_token = self._consume('IDENTIFIER', 'NUMBER', 'STRING')
        key_raw_value = key_candidate_token.value
        key_line = key_candidate_token.line
        
        key_for_node = key_raw_value # Default for NUMBER, IDENTIFIER
        if key_candidate_token.type == 'STRING': # Unquote if it was a string literal "key"
            key_for_node = key_raw_value[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        # If key_candidate_token.type is NUMBER, key_for_node remains string version (e.g. "10" for `10 = {...}`)
        # PdsNode `key` attribute can store it as int/float if needed, but comparison usually string-based.
        # For now, PdsNode.key stores what lexer gave (for numbers) or unquoted string.

        # After key candidate, check for '=', operator, or '{' (for named block not after equals)
        if self._is_next('EQUALS'):
            self._consume('EQUALS')
            # Now, value can be LBRACE (for block or list) or a primitive
            if self._is_next('LBRACE'): # key = { ... }
                return self._parse_block_or_list_after_equals(key_for_node, key_line)
            else: # key = primitive_value
                val_node_content = self._parse_primitive_value() # Parses primitive
                if val_node_content is None: # Should not happen if grammar is right
                    self._error("Expected a primitive value after '=' for KeyValuePair")
                
                # Check for same-line comment for the KVP
                comment_on_kvp_line = None
                # Check if next token is COMMENT and on same line as the *value* we just parsed.
                # Need to be careful with line numbers of multi-token values.
                # Simpler: if current token (after value) is COMMENT and its line is key_line (if value was single token)
                # or line of last token of value.
                # For now, assume primitive value is single token for comment association.
                # The token *before* current_token_index is the last token of the value.
                last_value_token = self.tokens[self.current_token_index -1] if self.current_token_index > 0 else key_candidate_token
                if self._is_next('COMMENT') and self._peek().line == last_value_token.line:
                    comment_on_kvp_line = self._consume('COMMENT').value
                return PdsKeyValuePair(key_for_node, val_node_content, key_line, comment_on_kvp_line)
        
        elif self._is_next('OPERATOR'): # key > value
            op_token = self._consume('OPERATOR')
            val_node_content = self._parse_value_for_kvp_or_op() # Parses primitive or anonymous block
            
            comment_on_op_line = None
            last_value_token = self.tokens[self.current_token_index -1] if self.current_token_index > 0 else op_token
            if self._is_next('COMMENT') and self._peek().line == last_value_token.line:
                 comment_on_op_line = self._consume('COMMENT').value
            return PdsOperatorCondition(key_for_node, op_token.value, val_node_content, key_line, comment_on_op_line)
        
        elif self._is_next('LBRACE'): # key { ... } (named block, not RHS of equals) - e.g. in Stellaris `modifier = { ... }`
            # LBRACE is *not* consumed yet by this path. _parse_block_content will consume it.
            return self._parse_block_content(key_for_node, key_line)
            
        else: # It was a bare primitive value (e.g. in a list: `my_list = { val1 val2 }`)
            # We already consumed key_candidate_token. We need to parse it as a primitive.
            # _parse_primitive_value can take token_val_if_consumed.
            # Need to know its original type (IDENTIFIER, NUMBER, STRING)
            # This is tricky. We use the already consumed token's value and type.
            primitive_val = None
            if key_candidate_token.type == 'IDENTIFIER':
                if key_raw_value.lower() == "yes": primitive_val = True
                elif key_raw_value.lower() == "no": primitive_val = False
                else: primitive_val = key_raw_value
            elif key_candidate_token.type == 'NUMBER':
                try: primitive_val = float(key_raw_value) if '.' in key_raw_value else int(key_raw_value)
                except ValueError: primitive_val = key_raw_value
            elif key_candidate_token.type == 'STRING':
                val_str = key_raw_value[1:-1]; primitive_val = val_str.replace('\\"', '"').replace('\\\\', '\\')
            
            if primitive_val is not None: return primitive_val # Return the raw parsed primitive
            else: self._error(f"Unexpected token sequence after '{key_raw_value}'. Expected '=', operator, or '{'{'}', or end of bare value.")
        return None # Should be unreachable


    def _parse_block_or_list_after_equals(self, key_str_for_node, key_line_for_node):
        # Current token is LBRACE, already peeked.
        lbrace_token = self._consume('LBRACE') # Consume the LBRACE
        
        comment_on_lbrace_line = None
        if self._is_next('COMMENT') and self._peek().line == lbrace_token.line:
            comment_on_lbrace_line = self._consume('COMMENT').value

        # Heuristic to distinguish list from block:
        # Look ahead for "identifier = " pattern vs. just "identifier" or "value".
        # Skip initial newlines/comments inside the braces.
        is_likely_block = False
        temp_idx = self.current_token_index # Current position after LBRACE and its line comment
        
        # Skip over newlines and comments to find the first significant token
        while temp_idx < len(self.tokens):
            token_at_temp = self.tokens[temp_idx]
            if token_at_temp.type in ('NEWLINE', 'COMMENT'):
                temp_idx += 1
            else: break # Found a significant token or EOF

        if temp_idx < len(self.tokens):
            first_significant_token_in_braces = self.tokens[temp_idx]
            if first_significant_token_in_braces.type in ('IDENTIFIER', 'STRING', 'NUMBER'):
                # If this is followed by '=', it's highly likely a KVP, so a block.
                if temp_idx + 1 < len(self.tokens) and self.tokens[temp_idx+1].type == 'EQUALS':
                    is_likely_block = True
                # If it's followed by OPERATOR, it's OpCond, so a block.
                elif temp_idx + 1 < len(self.tokens) and self.tokens[temp_idx+1].type == 'OPERATOR':
                    is_likely_block = True
                # If it's followed by LBRACE (e.g. child_key { ... }), it's a named block child, so parent is block.
                elif temp_idx + 1 < len(self.tokens) and self.tokens[temp_idx+1].type == 'LBRACE':
                    is_likely_block = True
            # If first_significant_token_in_braces is LBRACE itself (e.g. list_of_anon_blocks = { {b1} {b2} }),
            # then it's a list, not a block (unless this heuristic is changed).
            # Current heuristic treats this as list of anonymous blocks.
            # If it's RBRACE (empty {}), it's a block.
            elif first_significant_token_in_braces.type == 'RBRACE':
                is_likely_block = True # Empty {} is a block by default.

        if self._is_next('RBRACE'): # If, after consuming LBRACE and its comment, next is RBRACE (e.g. key = {})
             is_likely_block = True # Treat as empty block. List would need content or explicit non-block hint.


        if is_likely_block:
            # Parse as PdsBlock. _parse_block_content expects LBRACE to be *next*, but we consumed it.
            # So, we pass is_rhs_of_equals=True to tell it LBRACE was handled.
            block_node = self._parse_block_content(
                key_str_for_node, 
                key_line_for_node, 
                block_opening_comment=comment_on_lbrace_line, 
                is_rhs_of_equals=True # Indicates LBRACE already consumed
            )
            return block_node
        else: # Parse as PdsList
            list_node = PdsList(key_str_for_node, [], key_line_for_node, comment_on_lbrace_line)
            
            # Add list_node to parent_stack for indent calculation of its items if they are blocks
            # This is a bit ad-hoc; lists don't usually control indent of complex children like blocks do.
            # However, if a list contains anonymous blocks, those blocks need an indent context.
            # Let's assume list items get indent from the list's own indent level.
            current_parent_node = self.parent_stack[-1] if self.parent_stack else None
            list_node.indent_level = (current_parent_node.indent_level + 4) if current_parent_node else 0
            # self.parent_stack.append(list_node) # Don't add list to parent_stack, it's not a general child container like block

            while not self._is_next('RBRACE') and not self.is_eof():
                self._skip_newlines_and_comments_in_list_content() # Consume non-structural lines
                if self._is_next('RBRACE') or self.is_eof(): break # Check again after skipping

                list_item = self._parse_statement_or_item() # Parses bare primitive or anonymous block
                if list_item is not None:
                    # If item is a PdsNode (like an anonymous block), set its indent relative to list
                    if isinstance(list_item, PdsNode):
                        list_item.indent_level = list_node.indent_level + 4 # Or list_node.indent_level if items are at same level
                    list_node.values.append(list_item)
                elif not self._is_next('RBRACE'): # Should not happen if _parse_statement_or_item errors out
                    self._error("Expected a value, anonymous block, or RBRACE in list content")
            
            self._consume('RBRACE')
            # if self.parent_stack and self.parent_stack[-1] == list_node: self.parent_stack.pop()
            return list_node
            
    def _skip_newlines_and_comments_in_list_content(self):
        # In a list like `foo = { a b #comment \n c }`, we need to parse `a`, `b`, then skip `#comment`, `\n` to get to `c`.
        # This is different from top-level or block content where comments/newlines become nodes.
        # In lists, they are usually just separators.
        while self._is_next('NEWLINE') or self._is_next('COMMENT'):
            # If it's a comment, we might want to associate it with the *previous* list item if on same line.
            # For now, PdsList items don't store their own line comments. This simplifies things.
            # So, we just consume these tokens.
            self._advance() 
            if self.is_eof(): break


    def _parse_block_content(self, key_for_block, line_of_key, block_opening_comment=None, is_rhs_of_equals=False):
        # if is_rhs_of_equals is True, LBRACE and its line comment are assumed to be consumed already,
        # and block_opening_comment is passed in.
        # Otherwise, LBRACE is the current token.
        
        if not is_rhs_of_equals: # Standard block: key { or just {
            lbrace_token = self._consume('LBRACE')
            if self._is_next('COMMENT') and self._peek().line == lbrace_token.line:
                 block_opening_comment = self._consume('COMMENT').value
        
        block_node = PdsBlock(key_for_block, line_of_key, block_opening_comment)
        
        # Set indent level for this block
        current_parent_node = self.parent_stack[-1] if self.parent_stack else None
        block_node.indent_level = (current_parent_node.indent_level + 4) if current_parent_node else 0
        
        self.parent_stack.append(block_node)

        # Handling blank lines and comments inside a block
        last_node_line = line_of_key # Line of LBRACE or key
        
        while not self._is_next('RBRACE') and not self.is_eof():
            current_token_line = self._peek().line
            
            # Check for blank lines between last node and current token
            if current_token_line > last_node_line + 1:
                # There were one or more effectively blank lines
                for l_num in range(last_node_line + 1, current_token_line):
                    block_node.add_child(PdsBlankLine(l_num))
            
            if self._is_next('NEWLINE'):
                newline_token = self._advance() # Consume this newline
                last_node_line = newline_token.line
                # If next is also NEWLINE (on different line) or RBRACE/EOF, it's a blank line node
                next_peek = self._peek()
                if (next_peek.type == 'NEWLINE' and next_peek.line > newline_token.line) or \
                   next_peek.type == 'RBRACE' or next_peek.type == 'EOF':
                    # This newline token itself forms a blank line node.
                    block_node.add_child(PdsBlankLine(newline_token.line))
                    # last_node_line updated by this blank line for next iteration's check
                continue # Loop to process next token or blank lines
            
            # Now parse actual statement (KVP, OpCond, Comment, nested Block)
            child_node = self._parse_statement_or_item() # This will handle comments too
            if child_node:
                 if not isinstance(child_node, PdsNode):
                     # This can happen if _parse_statement_or_item returns a raw primitive,
                     # which shouldn't be a direct child of a block (must be KVP etc.)
                     self._error(f"Block '{key_for_block}' cannot directly contain primitive value '{child_node}'. Statement expected.")
                 
                 # Set child's indent relative to this block_node
                 child_node.indent_level = block_node.indent_level + 4
                 block_node.add_child(child_node)
                 last_node_line = self.tokens[self.current_token_index -1].line # Line of last token of child_node
            elif not self._is_next('RBRACE') and not self.is_eof(): # Should not happen if parser is correct
                 self._error(f"Parser stuck in block '{key_for_block}' before token", token_override=self._peek())
        
        self._consume('RBRACE')
        self.parent_stack.pop()
        return block_node

    def parse_file(self, filepath):
        self.root_nodes = []
        self.parent_stack = [] # Should be empty at root level
        self.current_token_index = 0

        if not os.path.exists(filepath):
            sys.stderr.write(f"Error: File not found: {filepath}\n")
            return []
        try:
            with open(filepath, 'r', encoding='utf-8-sig') as f: # Handles UTF-8 with BOM
                text_content = f.read()
        except UnicodeDecodeError: # Fallback to UTF-8 without BOM
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
            return []

        if not self.tokens or self.tokens[0].type == 'EOF': # Empty file or only EOF
            return []

        # --- Main parsing loop for top-level statements ---
        last_node_line = 0 # For blank line detection at root
        while not self.is_eof():
            current_token_line = self._peek().line

            if current_token_line > last_node_line + 1: # Blank lines at root
                for l_num in range(last_node_line + 1, current_token_line):
                    self.root_nodes.append(PdsBlankLine(l_num))
            
            if self._is_next('NEWLINE'):
                newline_token = self._advance()
                last_node_line = newline_token.line
                next_peek = self._peek()
                if (next_peek.type == 'NEWLINE' and next_peek.line > newline_token.line) or \
                    next_peek.type == 'EOF':
                     self.root_nodes.append(PdsBlankLine(newline_token.line))
                continue
            
            if self.is_eof(): break # Check after potential newline consumption

            # Parse a top-level statement (Comment, KVP, Block, List, OperatorCondition)
            try:
                node = self._parse_statement_or_item()
                if node:
                    if not isinstance(node, (PdsKeyValuePair, PdsOperatorCondition, PdsBlock, PdsList, PdsComment, PdsBlankLine)):
                        self._error(f"Root level statement parsed into unexpected type: {type(node)}. Value: {node}")
                    
                    # Root nodes have indent 0 unless part of some implicit global block not handled here
                    if isinstance(node, PdsNode): node.indent_level = 0
                    
                    self.root_nodes.append(node)
                    # Update last_node_line to the line of the last token that formed this node
                    # This is an approximation; precise end line of a complex node is harder.
                    # Use line of the token *before* current_token_index after parsing the node.
                    if self.current_token_index > 0:
                        last_node_line = self.tokens[self.current_token_index - 1].line
                    else: # Should not happen if node was parsed
                        last_node_line = current_token_line

                elif not self.is_eof(): # _parse_statement_or_item returned None but not EOF
                    self._error(f"Parser did not produce a node and is not at EOF.")
            
            except ValueError as parse_err: # Catch parsing errors from _error()
                sys.stderr.write(f"Parser error in {filepath}: {parse_err}\n")
                # Optionally, decide if parsing can continue or should stop.
                # For now, return what has been parsed so far.
                return self.root_nodes 
        
        return self.root_nodes

    @staticmethod
    def _nodes_to_string(nodes_list):
        output = []
        for node in nodes_list:
            # Root nodes typically have indent 0, but PdsBlock children will have their indent set.
            # PdsNode.to_string takes current_indent. For root nodes, this is 0.
            # For children within a block, their .to_string is called with parent_indent + 4.
            # So, node.indent_level should be authoritative if set.
            node_indent = node.indent_level if hasattr(node, 'indent_level') and node.indent_level is not None else 0
            output.append(node.to_string(node_indent)) 
        return "".join(output)


    def to_string(self): # Instance method to print parsed content
        return PdsParser._nodes_to_string(self.root_nodes)