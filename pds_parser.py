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
    # Pattern for unquoted identifiers: must be alphanumeric, underscore, dot, colon, at-sign, hyphen.
    # It must *fully* match the string to be considered an unquoted identifier.
    _lexer_identifier_pattern = re.compile(r'[\w\.:@\-]+')


    def __init__(self, key, value, line_number=-1, comment_text_on_line=None):
        super().__init__(line_number=line_number)
        self.key = key # string or number (parsed from IDENTIFIER or NUMBER token)
        self.value = value # primitive, or PdsBlock (for key = { ...block_content... })
        self.comment_text_on_line = comment_text_on_line

    def to_string(self, current_indent=0, is_inline_context=False):
        indent_str = " " * current_indent
        key_actual_str = str(self.key) 

        line_content_start = f"{indent_str}{key_actual_str} = "
        value_actual_str = ""
        ends_with_newline = True

        if isinstance(self.value, PdsBlock):
            # Pass current_indent to the block, and a strong hint that it can be inline.
            block_render_str = self.value.to_string(current_indent, is_inline_context=True)
            
            # Check if the block rendered itself inline (no newlines).
            # The .strip() is important to remove leading/trailing newlines if block renders multiline.
            if "\n" not in block_render_str.strip(): 
                value_actual_str = block_render_str.strip() 
            else: # Multiline block value
                value_actual_str = block_render_str # Block's to_string handles its own newlines and indentation.
                ends_with_newline = False 
        elif isinstance(self.value, str):
            # Conditions for quoting string values in PDS:
            # 1. Contains whitespace (space, tab)
            # 2. Contains reserved characters: # = { } "
            # 3. Is an empty string ""
            # 4. Does not fully match the simple identifier pattern (e.g., "foo bar", "foo.bar.baz")
            #    PDS often unquotes "foo.bar" if it's a valid identifier.
            #    The `_lexer_identifier_pattern` is designed for these.
            
            # Special cases that are usually unquoted even if they might contain special characters or be keywords
            # (e.g., 'yes', 'no', paths like '@foo' in `icon = @foo`)
            is_special_unquoted_keyword = self.value.lower() in ["yes", "no", "rgb", "hsv", "hsv360", "root", "prev", "owner"]
            is_icon_path_literal = self.key == "icon" and self.value.startswith("@") # @-paths are usually unquoted
            
            # Check if the value *itself* is a simple identifier (no quotes needed)
            is_simple_identifier_fully = self._lexer_identifier_pattern.fullmatch(self.value) is not None

            # Determine if quotes are needed:
            needs_quoting = False
            if not is_simple_identifier_fully: # If it's not a simple identifier (e.g. contains spaces or forbidden chars)
                needs_quoting = True
            elif not self.value: # Empty string must be quoted
                needs_quoting = True
            # Special keywords/paths generally don't need quoting unless they *also* contain forbidden chars (rare in game files).
            if is_special_unquoted_keyword or is_icon_path_literal:
                needs_quoting = False # Override if it's a special unquoted case

            if needs_quoting:
                # Escape backslashes and double quotes
                escaped_value = self.value.replace('\\', '\\\\').replace('"', '\\"')
                value_actual_str = f'"{escaped_value}"'
            else:
                value_actual_str = self.value
        elif isinstance(self.value, bool):
            value_actual_str = "yes" if self.value else "no"
        else: # Numbers (int, float) or other types
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

    def to_string(self, current_indent=0, is_inline_context=False): 
        indent_str = " " * current_indent
        
        # Heuristic for inline vs. multi-line:
        # Multi-line if:
        #   - Any value is a PdsNode (e.g., an anonymous block)
        #   - The list has more than a few simple values (e.g., > 5)
        #   - The combined string length of inline simple values exceeds a threshold.
        # Otherwise, attempt inline. Empty lists are always inline.
        is_multiline = False
        if any(isinstance(v, PdsNode) for v in self.values):
            is_multiline = True
        elif len(self.values) > 5:
            is_multiline = True
        elif len(self.values) > 0 and len(" ".join([str(v) for v in self.values if not isinstance(v, PdsNode)])) > 70:
            is_multiline = True
        
        if not self.values: # Empty list is always inline
            is_multiline = False

        if not is_multiline: # Inline format: key = { val1 val2 "val 3" }
            formatted_values = []
            for v_item in self.values: 
                val_str_item = ""
                if isinstance(v_item, str):
                    # Use PdsKeyValuePair's robust quoting logic
                    is_simple_id = PdsKeyValuePair._lexer_identifier_pattern.fullmatch(v_item) is not None
                    is_bool_like_keyword = v_item.lower() in ["yes", "no"] 
                    
                    needs_q = not is_simple_id or \
                                any(c in v_item for c in ' \t#={}"') or \
                                not v_item 
                    
                    if is_bool_like_keyword and is_simple_id: # yes/no are typically unquoted
                        val_str_item = v_item
                    elif needs_q: # Needs quotes
                        val_str_item = f'"{v_item.replace("\\", "\\\\").replace("\"", "\\\"")}"'
                    else: # Simple identifier, no quotes
                        val_str_item = v_item
                elif isinstance(v_item, bool):
                    val_str_item = "yes" if v_item else "no"
                else: # Numbers
                    val_str_item = str(v_item)
                formatted_values.append(val_str_item)
            
            value_str = " ".join(formatted_values)
            inner_content = f" {value_str} " if value_str else " " 
            base_string = f"{indent_str}{self.key} = {{{inner_content}}}"
            if self.comment_text_on_line:
                base_string += f" # {self.comment_text_on_line}"
            return base_string + "\n"
        else: # Multi-line format
            output_lines = []
            open_brace_line = f"{indent_str}{self.key} = {{"
            if self.comment_text_on_line:
                open_brace_line += f" # {self.comment_text_on_line}"
            output_lines.append(open_brace_line + "\n")

            child_render_indent = current_indent + 4 
            for value_item in self.values:
                if isinstance(value_item, PdsNode): # Anonymous blocks or other structured nodes
                    value_item.indent_level = child_render_indent 
                    # Pass is_inline_context=False, as children of multi-line lists usually aren't inline.
                    output_lines.append(value_item.to_string(child_render_indent, is_inline_context=False))
                else: # Primitive value
                    val_str_item = "" 
                    if isinstance(value_item, str):
                        is_simple_id = PdsKeyValuePair._lexer_identifier_pattern.fullmatch(value_item) is not None
                        is_bool_like_keyword = value_item.lower() in ["yes", "no"]
                        needs_q = not is_simple_id or any(c in value_item for c in ' \t#={}"') or not value_item
                        if is_bool_like_keyword and is_simple_id: val_str_item = value_item
                        elif needs_q: val_str_item = f'"{value_item.replace("\\", "\\\\").replace("\"", "\\\"")}"'
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
        if self.key is not None: 
            block_key_prefix = f"{str(self.key)} = "

        # Heuristic for inline rendering: key = { child_key = val }
        # This is primarily for blocks that are values of KVPs (is_inline_context=True).
        # Allow comments if the inline form is still compact.
        can_attempt_inline_render = is_inline_context and len(self.children) == 1 and \
                                   isinstance(self.children[0], PdsKeyValuePair) and \
                                   not isinstance(self.children[0].value, PdsBlock) and \
                                   len(str(self.children[0].key)) + len(str(self.children[0].value)) < 80 # Increased length tolerance

        if can_attempt_inline_render:
            child_kvp_node = self.children[0]
            child_str_compact = child_kvp_node.to_string(current_indent=0).strip() # Render child with 0 indent and strip.
            
            # If the child KVP's string form is truly a single line (no internal newlines), use it.
            if "\n" not in child_str_compact:
                block_output = f"{block_key_prefix}{{{child_str_compact}}}"
                if self.comment_text_on_line:
                    block_output += f" # {self.comment_text_on_line}"
                return block_output 

        # Standard multi-line rendering
        output_parts = []
        open_brace_line = f"{indent_str}{block_key_prefix}{{"
        if self.comment_text_on_line:
            open_brace_line += f" # {self.comment_text_on_line}"
        output_parts.append(open_brace_line) 

        if not self.children: 
            output_parts.append("}\n") 
        else:
            output_parts.append("\n") 
            child_render_indent = current_indent + 4
            for child_node in self.children:
                child_node.indent_level = child_render_indent 
                output_parts.append(child_node.to_string(child_render_indent, is_inline_context=False)) 
            output_parts.append(f"{indent_str}}}\n") 
        
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
        # This part handles peeking beyond the end. If tokens is empty at this point, it's an error.
        # With the fix below, self.tokens should always contain at least an EOF token if parsing began.
        if self.tokens and self.tokens[-1].type == 'EOF':
            return self.tokens[-1]
        raise IndexError("Parser internal error: Attempted to peek into an empty token list or beyond EOF without a valid EOF token.")

    def _advance(self):
        if self.current_token_index < len(self.tokens) - 1: # Stop advancing at EOF
            self.current_token_index += 1
        return self.tokens[self.current_token_index -1] # Return the token just consumed

    def _consume(self, *expected_types):
        token = self._peek()
        if token.type in expected_types:
            return self._advance()
        self._error(f"Expected one of {expected_types} but got {token.type}", token_override=token)

    def _is_next(self, *token_types, offset=0): # <--- ADD offset=0 here
        """
        Checks if the token at the given offset matches any of the expected types.
        """
        return self._peek(offset).type in token_types # <--- Pass offset to _peek()

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
            if val_to_parse.lower() == "yes": parsed_value = True
            elif val_to_parse.lower() == "no": parsed_value = False
            else: parsed_value = val_to_parse 
        elif type_of_val == 'NUMBER':
            try: parsed_value = float(val_to_parse) if '.' in val_to_parse else int(val_to_parse)
            except ValueError: parsed_value = val_to_parse
        elif type_of_val == 'STRING':
            val_str = val_to_parse[1:-1]
            parsed_value = val_str.replace('\\"', '"').replace('\\\\', '\\')
        
        return parsed_value

    def _parse_value_for_kvp_or_op(self): 
        if self._is_next('LBRACE'):
            # This is an anonymous block as a value.
            return self._parse_block_content(key_for_block=None, line_of_key=self._peek().line)
        else:
            token = self._peek() 
            parsed_value = self._parse_primitive_value(token) 
            if parsed_value is not None:
                self._advance() # Consume the token now that it's parsed as a primitive value
                return parsed_value
            self._error(f"Expected a primitive value or an anonymous block {{...}} as RHS value")
        return None

    def _parse_statement(self):
        start_token_line = self._peek().line

        if self._is_next('LBRACE'): # Anonymous block
            return self._parse_block_content(key_for_block=None, line_of_key=start_token_line)

        if not (self._is_next('IDENTIFIER') or self._is_next('NUMBER') or self._is_next('STRING')):
             self._error("Statement must start with IDENTIFIER, NUMBER, STRING, or LBRACE for anonymous block")

        key_candidate_token = self._consume('IDENTIFIER', 'NUMBER', 'STRING')
        key_raw_value = key_candidate_token.value
        key_line = key_candidate_token.line
        
        key_for_node = key_raw_value
        if key_candidate_token.type == 'STRING':
            key_for_node = key_raw_value[1:-1].replace('\\"', '"').replace('\\\\', '\\')

        if self._is_next('EQUALS'):
            self._consume('EQUALS')
            if self._is_next('LBRACE'): # Found pattern: key = { ... }
                # In this case, the '{...}' part is the VALUE of a KeyValuePair.
                # _parse_block_or_list_after_equals will return the PdsBlock or PdsList object.
                value_node = self._parse_block_or_list_after_equals(key_for_node, key_line) # Returns PdsBlock or PdsList
                
                comment_on_kvp_line = None
                # Check for same-line comment immediately after the closing brace of the block/list
                # The token *before* current_token_index is the '}' of the block/list.
                last_value_token_line = self.tokens[self.current_token_index -1].line if self.current_token_index > 0 else key_line
                if self._is_next('COMMENT') and self._peek().line == last_value_token_line:
                    comment_on_kvp_line = self._consume('COMMENT').value
                
                return PdsKeyValuePair(key_for_node, value_node, key_line, comment_on_kvp_line)
            else: # key = primitive_value
                val_node_content = self._parse_value_for_kvp_or_op() # This consumes its token
                
                comment_on_kvp_line = None
                if self._is_next('COMMENT') and self._peek().line == key_line:
                    comment_on_kvp_line = self._consume('COMMENT').value
                return PdsKeyValuePair(key_for_node, val_node_content, key_line, comment_on_kvp_line)
        
        elif self._is_next('OPERATOR'): # key > value
            # ... (this part remains unchanged) ...
            op_token = self._consume('OPERATOR')
            val_node_content = self._parse_value_for_kvp_or_op() # This consumes its token
            
            comment_on_op_line = None
            if self._is_next('COMMENT') and self._peek().line == key_line:
                 comment_on_op_line = self._consume('COMMENT').value
            return PdsOperatorCondition(key_for_node, op_token.value, val_node_content, key_line, comment_on_op_line)
        
        elif self._is_next('LBRACE'): # key { ... } (named block, not RHS of equals)
            # This is specifically for syntax like `modifier = { ... }` in CK3, where no `=` is present.
            # It directly returns a PdsBlock node.
            return self._parse_block_content(key_for_node, key_line)
            
        else: # Bare primitive value (e.g. in a list: `my_list = { val1 val2 }`)
            # ... (this part remains unchanged) ...
            primitive_val = self._parse_primitive_value(key_candidate_token)
            if primitive_val is not None: return primitive_val
            else: self._error(f"Unexpected token sequence after '{key_raw_value}'. Expected '=', operator, or '{{', or bare value (if in list).")
        return None
    
    def _parse_block_or_list_after_equals(self, key_str_for_node, key_line_for_node):
        lbrace_token = self._consume('LBRACE')
        
        comment_on_lbrace_line = None
        if self._is_next('COMMENT') and self._peek().line == lbrace_token.line:
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
        
        # If the block is empty, it's a PdsBlock by convention.
        if self._is_next('RBRACE', offset=temp_idx - self.current_token_index):
            return self._parse_block_content(key_str_for_node, key_line_for_node, block_opening_comment=comment_on_lbrace_line, is_rhs_of_equals=True)

        is_likely_block = False
        if temp_idx < len(self.tokens):
            first_significant_token_in_braces = self.tokens[temp_idx]
            if first_significant_token_in_braces.type in ('IDENTIFIER', 'STRING', 'NUMBER'):
                # If first item is followed by '=', 'OPERATOR', or '{' (for a named sub-block), it's a block.
                if temp_idx + 1 < len(self.tokens):
                    next_token = self.tokens[temp_idx+1]
                    if next_token.type in ('EQUALS', 'OPERATOR', 'LBRACE'):
                        is_likely_block = True
            elif first_significant_token_in_braces.type == 'LBRACE':
                # If the first *significant* token is LBRACE, it's an anonymous block.
                # A list like `foo = { {item1} {item2} }` contains anonymous blocks, so it's a PdsList.
                is_likely_block = False # This explicitly says it's a list if it starts with an anonymous block.
        
        if is_likely_block:
            return self._parse_block_content(key_str_for_node, key_line_for_node, block_opening_comment=comment_on_lbrace_line, is_rhs_of_equals=True)
        else: # Parse as PdsList
            list_node = PdsList(key_str_for_node, [], key_line_for_node, comment_on_lbrace_line)
            
            while not self._is_next('RBRACE') and not self.is_eof():
                # For lists, comments and newlines between items are usually just separators, not independent nodes.
                # Consume them here without adding them to `list_node.values`.
                self._skip_newlines_and_comments_in_list_content() 
                if self._is_next('RBRACE') or self.is_eof(): break

                list_item = self._parse_statement() # Can return primitive values here.
                if list_item is not None:
                    if isinstance(list_item, PdsNode):
                        # Set indent level for structured items within a list
                        # It should be relative to the parent block's indent level, not the list's own.
                        list_item.indent_level = self.parent_stack[-1].indent_level + 4 if self.parent_stack else 4
                    list_node.values.append(list_item)
                elif not self._is_next('RBRACE'): 
                    self._error("Expected a value, anonymous block, or RBRACE in list content")
            
            self._consume('RBRACE')
            return list_node
            
    def _skip_newlines_and_comments_in_list_content(self):
        # Consume any `NEWLINE` or `COMMENT` tokens until a significant token or RBRACE/EOF is found.
        # These are treated as ignored whitespace/comments *within* a list, not parsed as nodes.
        while self._is_next('NEWLINE') or self._is_next('COMMENT'):
            self._advance()
            if self.is_eof(): break

    def _parse_block_content(self, key_for_block, line_of_key, block_opening_comment=None, is_rhs_of_equals=False):
        if not is_rhs_of_equals:
            lbrace_token = self._consume('LBRACE')
            if self._is_next('COMMENT') and self._peek().line == lbrace_token.line:
                 block_opening_comment = self._consume('COMMENT').value
        
        block_node = PdsBlock(key_for_block, line_of_key, block_opening_comment)
        
        current_parent_node = self.parent_stack[-1] if self.parent_stack else None
        block_node.indent_level = (current_parent_node.indent_level + 4) if current_parent_node else 0
        
        self.parent_stack.append(block_node)

        last_parsed_line = line_of_key # Track the line number of the last consumed token/node.

        while not self._is_next('RBRACE') and not self.is_eof():
            current_token_line = self._peek().line

            # Handle blank lines by comparing current token's line with last parsed token's line
            if current_token_line > last_parsed_line + 1:
                for l_num in range(last_parsed_line + 1, current_token_line):
                    block_node.add_child(PdsBlankLine(l_num))
            
            # Consume all `NEWLINE` tokens that follow directly (e.g., in a sequence of blank lines)
            while self._is_next('NEWLINE'):
                last_parsed_line = self._advance().line 
            
            if self._is_next('RBRACE') or self.is_eof(): break 

            if self._is_next('COMMENT'): # Comments are explicit nodes within blocks
                comment_node = self._parse_comment_node()
                comment_node.indent_level = block_node.indent_level + 4
                block_node.add_child(comment_node)
                last_parsed_line = comment_node.line_number 
                continue 
            
            # Parse an actual statement (KVP, OpCond, nested Block, or List)
            child_node = self._parse_statement() 
            if child_node:
                 if not isinstance(child_node, PdsNode):
                     self._error(f"Block '{key_for_block}' cannot directly contain bare primitive value '{child_node}'. KVP, OpCond, or Block expected.")
                 
                 child_node.indent_level = block_node.indent_level + 4
                 block_node.add_child(child_node)
                 last_parsed_line = self.tokens[self.current_token_index - 1].line # Line of the last token consumed for this node
            elif not self._is_next('RBRACE') and not self.is_eof(): 
                 self._error(f"Parser stuck in block '{key_for_block}' before token", token_override=self._peek())
        
        self._consume('RBRACE')
        self.parent_stack.pop()
        return block_node

    def parse_file(self, filepath):
        self.root_nodes = []
        self.parent_stack = []
        self.current_token_index = 0
        self.tokens = [] # <--- ADD THIS LINE: Reset tokens for each new parse operation

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

        # Handle truly empty files or files containing only EOF token after lexing
        if not self.tokens or (len(self.tokens) == 1 and self.tokens[0].type == 'EOF'):
             return [] # Return empty list of nodes, as no actual content was parsed.

        # --- Main parsing loop for top-level statements ---
        last_parsed_line = 0 # Track line of last parsed *node* for blank line detection at root
        
        # The loop condition `while not self.is_eof():` is now safe because `self.tokens`
        # is guaranteed to have at least an EOF token at this point (or it returned earlier).
        while not self.is_eof():
            current_token_line = self._peek().line

            # Handle blank lines before the next statement
            if current_token_line > last_parsed_line + 1:
                for l_num in range(last_parsed_line + 1, current_token_line):
                    self.root_nodes.append(PdsBlankLine(l_num))
            
            # Consume all `NEWLINE` tokens that follow directly
            while self._is_next('NEWLINE'):
                last_parsed_line = self._advance().line 
            
            if self.is_eof(): break 

            if self._is_next('COMMENT'): # Top-level comments are explicit nodes
                comment_node = self._parse_comment_node()
                comment_node.indent_level = 0
                self.root_nodes.append(comment_node)
                last_parsed_line = comment_node.line_number
                continue

            try:
                node = self._parse_statement() 
                if node:
                    if not isinstance(node, PdsNode): 
                        self._error(f"Root level cannot contain bare primitive value: {node}")
                    
                    node.indent_level = 0
                    self.root_nodes.append(node)
                    if self.current_token_index > 0:
                        last_parsed_line = self.tokens[self.current_token_index - 1].line
                    else:
                        last_parsed_line = current_token_line

                elif not self.is_eof(): 
                    self._error(f"Parser did not produce a node and is not at EOF.")
            
            except ValueError as parse_err: 
                sys.stderr.write(f"Parser error in {filepath}: {parse_err}\n")
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