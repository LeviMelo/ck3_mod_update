import re
import os
import sys # For stderr in __eq__

# --- PdsToken and PdsLexer (UNCHANGED) ---

class PdsToken:
    """Represents a token found during lexical analysis."""
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
    """
    Lexer for PDS (Crusader Kings III) script files.
    Breaks input text into a stream of tokens.
    """
    def __init__(self, text):
        self.text = text
        self.pos = 0
        self.line = 1
        self.column = 1
        self.tokens = []

        self.single_char_tokens = {
            '{': 'LBRACE',
            '}': 'RBRACE',
            '=': 'EQUALS',
        }
        
        # Adjusted IDENTIFIER to be more permissive as per Jomini spec (includes dots, colons etc.)
        self.patterns = [
            ('COMMENT', r'#.*'), 
            ('OPERATOR', r'(?:>=|<=|==|!=|\?=|>|<|!)'), # \?= for script operator
            ('STRING', r'"(?:\\.|[^"\\])*"'), # Handles escaped quotes and backslashes
            ('NUMBER', r'-?\d+(?:\.\d+)?'), # Allows for floats and integers
            ('IDENTIFIER', r'[\w\.:@\-]+(?:[\w\.:@\-]*[\w\.:@\-])?'), # More permissive identifier
        ]
        
        self.compiled_patterns = [(k, re.compile(v)) for k, v in self.patterns]

    def _advance(self, count):
        for _ in range(count):
            if self.pos >= len(self.text):
                break
            char_being_advanced_over = self.text[self.pos]
            self.pos += 1
            if char_being_advanced_over == '\n':
                self.line += 1
                self.column = 1
            else:
                self.column += 1

    def _add_token_and_advance(self, token_type, token_value_for_storage, advance_length):
        self.tokens.append(PdsToken(token_type, token_value_for_storage, self.line, self.column))
        self._advance(advance_length)

    def tokenize(self):
        while self.pos < len(self.text):
            char = self.text[self.pos]
            error_reporting_column = self.column 
            
            if char.isspace():
                # Consume all whitespace, but specifically add NEWLINE tokens
                start_pos = self.pos
                while self.pos < len(self.text) and self.text[self.pos].isspace():
                    if self.text[self.pos] == '\n':
                        self._add_token_and_advance('NEWLINE', '\n', 1)
                    else:
                        self._advance(1) # For other whitespace like space/tab
                continue

            if char in self.single_char_tokens:
                self._add_token_and_advance(self.single_char_tokens[char], char, 1)
                continue

            matched = False
            for token_type, pattern_regex in self.compiled_patterns:
                match = pattern_regex.match(self.text, self.pos)
                if match:
                    token_value_raw = match.group(0)
                    if token_type == 'COMMENT':
                        self._add_token_and_advance(token_type, token_value_raw[1:], len(token_value_raw)) # Store comment text without #
                    elif token_type == 'STRING':
                        # Store string content without quotes, handling escapes later if needed by parser
                        # For now, keep raw string content with quotes for simplicity unless parser handles unescaping.
                        # The PdsParser._parse_value handles unquoting.
                        self._add_token_and_advance(token_type, token_value_raw, len(token_value_raw))
                    else:
                        self._add_token_and_advance(token_type, token_value_raw, len(token_value_raw))
                    matched = True
                    break 

            if matched:
                continue 

            raise ValueError(f"Lexer Error: Unexpected character '{char}' at line {self.line}, column {error_reporting_column}")
        
        self.tokens.append(PdsToken('EOF', '', self.line, self.column)) 
        return self.tokens

class PdsNode:
    def __init__(self, line_number=-1): 
        self.line_number = line_number
        self.indent_level = 0 
        self.comment_text_on_line = None # Comment on the same line as the node's primary definition
        # self._cached_hash = None # Hash memoization removed for now due to complexity with shallow/deep

    def to_string(self, current_indent=0, is_inline_context=False):
        raise NotImplementedError(f"to_string() not implemented for {self.__class__.__name__}")

    def __repr__(self):
        key_repr = getattr(self, 'key', 'N/A')
        if not isinstance(key_repr, str): key_repr = str(key_repr)
        comment_repr = self.comment_text_on_line
        comment_str = f", CommentText='{comment_repr[:20]}...'" if comment_repr else ""
        
        display_class_name = self.__class__.__name__
        specific_repr = ""
        if isinstance(self, PdsComment): specific_repr = f"Comment: '{getattr(self, 'comment_text', '')[:30]}...'"
        elif isinstance(self, PdsBlankLine): specific_repr = "Blank Line"
        elif isinstance(self, PdsList): specific_repr = f"{key_repr}={{...}}"
        elif isinstance(self, PdsOperatorCondition): specific_repr = f"{key_repr} {getattr(self, 'operator','OP')} {getattr(self,'value','VAL')}"
        elif isinstance(self, PdsBlock): specific_repr = f"{key_repr}={{...}}"
        elif isinstance(self, PdsKeyValuePair) and isinstance(getattr(self,'value',None), PdsBlock): specific_repr = f"{key_repr}={{...}}"
        else: specific_repr = f"K='{key_repr}'"
        
        return f"<{display_class_name} L{self.line_number} I{self.indent_level} {specific_repr}{comment_str}>"

    def _base_copy_attrs(self, new_node):
        new_node.line_number = self.line_number # Copy line number for reference, though not used in equality
        new_node.indent_level = self.indent_level
        new_node.comment_text_on_line = self.comment_text_on_line
        return new_node

    def copy(self): 
        raise NotImplementedError(f"copy() not implemented for {self.__class__.__name__}")

    def get_structural_components(self, shallow_block=False):
        raise NotImplementedError(f"get_structural_components() not implemented for {self.__class__.__name__}")

    def __eq__(self, other):
        """
        Compares two PdsNode objects for structural equality.
        This method is called by Python's == operator.
        It does NOT take shallow_block_comparison. The type of comparison (shallow/deep for blocks)
        is determined by get_comparator_key for SequenceMatcher, or by this method's internal
        logic if it needs to compare children.
        """
        if other is None:
            return False
        if self.__class__ is not other.__class__:
            # If different PdsNode subclasses, they are not structurally equal.
            # Returning False is better than NotImplemented for direct boolean contexts.
            return False
        
        # If they are the same class, compare based on their structural components.
        # For PdsBlock, its get_structural_components will handle children deeply.
        # Line number and indent level are ignored for structural equality by design.
        try:
            return self.get_structural_components(shallow_block=False) == \
                   other.get_structural_components(shallow_block=False)
        except NotImplementedError as e:
            sys.stderr.write(f"ERROR: __eq__ called on class without get_structural_components: {e}\n")
            return False # Or re-raise, but False is safer for comparisons

    def __ne__(self, other): # Explicitly define __ne__
        equal = self.__eq__(other)
        return False if equal is NotImplemented else not equal

    def __hash__(self):
        # Hash should be based on the same components as __eq__ (deep equality)
        # PdsDiffer will use get_comparator_key for SequenceMatcher, which provides its own hashable tuple.
        try:
            return hash(self.get_structural_components(shallow_block=False))
        except NotImplementedError:
            # Fallback, should not happen if all subclasses implement get_structural_components
            return id(self) 
        
    def get_comparator_key(self, shallow_block_for_seq_matcher=False):
        """
        Provides a key for SequenceMatcher. This key IS HASHABLE by SequenceMatcher.
        It uses get_structural_components with the shallow_block_for_seq_matcher flag.
        """
        return self.get_structural_components(shallow_block=shallow_block_for_seq_matcher)

class PdsComment(PdsNode):
    def __init__(self, comment_text, line_number=-1):
        super().__init__(line_number=line_number)
        self.comment_text = comment_text # Text after #

    def to_string(self, current_indent=0, is_inline_context=False): # is_inline_context usually false for comments
        indent_str = " " * current_indent
        # If comment_text_on_line exists for the comment node itself, it's a bit meta.
        # Usually, comments are either full-line or on the line of another node.
        # This class represents a full-line comment.
        return f"{indent_str}#{self.comment_text}\n"

    def copy(self):
        new_node = PdsComment(comment_text=self.comment_text, line_number=self.line_number)
        return self._base_copy_attrs(new_node)

    def get_structural_components(self, shallow_block=False): # shallow_block irrelevant
        return (self.__class__.__name__, self.comment_text)

class PdsBlankLine(PdsNode):
    def __init__(self, line_number=-1):
        super().__init__(line_number=line_number)

    def to_string(self, current_indent=0, is_inline_context=False):
        return "\n" 

    def copy(self):
        new_node = PdsBlankLine(line_number=self.line_number)
        return self._base_copy_attrs(new_node)

    def get_structural_components(self, shallow_block=False): # shallow_block irrelevant
        return (self.__class__.__name__,)

class PdsKeyValuePair(PdsNode):
    _lexer_identifier_pattern = r'[\w\.:@\-]+(?:[\w\.:@\-]*[\w\.:@\-])?' # From lexer

    def __init__(self, key, value, line_number=-1, comment_text_on_line=None):
        super().__init__(line_number=line_number)
        self.key = key
        self.value = value # Can be primitive (str, int, float, bool) or PdsBlock
        self.comment_text_on_line = comment_text_on_line

    def to_string(self, current_indent=0, is_inline_context_ignored=False):
        indent_str = " " * current_indent
        line_content_start = f"{indent_str}{self.key} = "
        value_actual_str = ""
        line_ending = "\n"

        if isinstance(self.value, PdsBlock):
            block_node = self.value
            # Pass current_indent to ensure children of block_node are indented correctly relative to block_node's line
            block_render_str = block_node.to_string(current_indent, is_inline_context=True) # Try inline
            
            # If block_render_str is indeed inline (single line, starts with '{', no internal newlines)
            if block_render_str.strip().startswith("{") and block_render_str.strip().endswith("}") and "\n" not in block_render_str:
                value_actual_str = block_render_str.strip() #  e.g. { child=1 }
            else: # Block is multi-line
                # The block.to_string will handle its own indentation and newlines.
                # We just need to append it. If it starts with the same indent, strip it to avoid double indent.
                if block_render_str.startswith(indent_str): # Should not happen if block.to_string uses current_indent=0 for inline values
                     value_actual_str = block_render_str[len(indent_str):].lstrip()
                else:
                     value_actual_str = block_render_str.lstrip() # Remove leading spaces if any from block string
                line_ending = "" # Block provides its own final newline
        elif isinstance(self.value, str):
            # Quoting logic based on Jomini/Clausewitz: quote if contains space, special chars, or is empty
            # or not a simple identifier.
            is_simple_identifier = re.fullmatch(PdsKeyValuePair._lexer_identifier_pattern, self.value)
            needs_quoting = not is_simple_identifier or \
                            any(c in self.value for c in ' \t#={}"') or \
                            not self.value or self.value.lower() in ["yes", "no"] # "yes" and "no" are identifiers not strings
            
            if self.value.lower() in ["yes", "no"]: # Booleans are unquoted identifiers
                 value_actual_str = self.value
            elif needs_quoting:
                # Simple escape for quotes and backslashes within the string
                escaped_value = self.value.replace('\\', '\\\\').replace('"', '\\"')
                value_actual_str = f'"{escaped_value}"'
            else:
                value_actual_str = self.value
        elif isinstance(self.value, bool):
            value_actual_str = "yes" if self.value else "no"
        else: 
            value_actual_str = str(self.value) # Numbers
        
        line_content = line_content_start + value_actual_str
        if self.comment_text_on_line: 
            line_content += f" # {self.comment_text_on_line}"
        return line_content + line_ending

    def copy(self):
        new_value = self.value
        if isinstance(self.value, PdsNode):
            new_value = self.value.copy()
        # Primitives (str, int, float, bool) are copied by assignment
        
        new_node = PdsKeyValuePair(
            key=self.key, 
            value=new_value,
            line_number=self.line_number,
            comment_text_on_line=self.comment_text_on_line
        )
        return self._base_copy_attrs(new_node)

    def get_structural_components(self, shallow_block=False):
        value_comp = self.value
        if isinstance(self.value, PdsNode):
            # If value is a PdsBlock, shallow_block propagates for its comparison
            value_comp = self.value.get_structural_components(shallow_block=shallow_block)
        # Primitives are compared directly
        return (self.__class__.__name__, self.key, value_comp, self.comment_text_on_line)

class PdsList(PdsNode): 
    # Represents foo = { val1 val2 val3 } or foo = { "val 1" val2 }
    # As per Jomini spec, these are distinct from blocks.
    # The key difference in parsing is the absence of 'key =' for items inside.
    def __init__(self, key, values, line_number=-1, comment_text_on_line=None): 
        super().__init__(line_number=line_number)
        self.key = key # The key of the list itself, e.g., "my_list_key" in "my_list_key = { ... }"
        self.values = values # Expected to be list of primitives (str, int, float, bool)
        self.comment_text_on_line = comment_text_on_line

    def to_string(self, current_indent=0, is_inline_context=False): # is_inline_context can be true
        indent_str = " " * current_indent
        formatted_values = []
        for v_item in self.values:
            if isinstance(v_item, str):
                is_simple_identifier = re.fullmatch(PdsKeyValuePair._lexer_identifier_pattern, v_item)
                needs_quoting = not is_simple_identifier or \
                                any(c in v_item for c in ' \t#={}"') or \
                                not v_item or v_item.lower() in ["yes", "no"]
                if v_item.lower() in ["yes", "no"]: # booleans
                    formatted_values.append(v_item)
                elif needs_quoting:
                    escaped_value = v_item.replace('\\', '\\\\').replace('"', '\\"')
                    formatted_values.append(f'"{escaped_value}"')
                else:
                    formatted_values.append(v_item)
            elif isinstance(v_item, bool):
                 formatted_values.append("yes" if v_item else "no")
            else: # numbers
                formatted_values.append(str(v_item))

        value_str = " ".join(formatted_values) 
        # Jomini spec: lists can be empty {} or have values { v1 v2 }
        # Inline format: key = { v1 v2 }
        # Multi-line format:
        # key = {
        #    v1
        #    v2
        # }
        # For simplicity, always render inline for now unless too long (not implemented)
        
        inner_content = f" {value_str} " if self.values else " " 
        base_string = f"{indent_str}{self.key} = {{{inner_content}}}"
        if self.comment_text_on_line:
            base_string += f" # {self.comment_text_on_line}"
        return base_string + "\n"

    def copy(self):
        new_node = PdsList(
            key=self.key,
            values=list(self.values), # Values are primitives, list() creates a shallow copy
            line_number=self.line_number,
            comment_text_on_line=self.comment_text_on_line
        )
        return self._base_copy_attrs(new_node)

    def get_structural_components(self, shallow_block=False): # shallow_block irrelevant
        # PDS lists ARE ordered. Do not sort self.values for comparison.
        return (self.__class__.__name__, self.key, tuple(self.values), self.comment_text_on_line)

class PdsOperatorCondition(PdsNode): # e.g. age > 16
    def __init__(self, key, operator, value, line_number=-1, comment_text_on_line=None):
        super().__init__(line_number=line_number)
        self.key = key # Left-hand side (identifier)
        self.operator = operator 
        self.value = value # Right-hand side (can be primitive or identifier, rarely a block)     
        self.comment_text_on_line = comment_text_on_line

    def to_string(self, current_indent=0, is_inline_context_ignored=False):
        indent_str = " " * current_indent
        # Value formatting (quoting) similar to PdsKeyValuePair
        value_actual_str = ""
        if isinstance(self.value, str):
            is_simple_identifier = re.fullmatch(PdsKeyValuePair._lexer_identifier_pattern, self.value)
            needs_quoting = not is_simple_identifier or \
                            any(c in self.value for c in ' \t#={}"') or \
                            not self.value or self.value.lower() in ["yes", "no"]
            if self.value.lower() in ["yes", "no"]:
                 value_actual_str = self.value
            elif needs_quoting:
                escaped_value = self.value.replace('\\', '\\\\').replace('"', '\\"')
                value_actual_str = f'"{escaped_value}"'
            else:
                value_actual_str = self.value
        elif isinstance(self.value, bool):
            value_actual_str = "yes" if self.value else "no"
        else: # numbers or other PdsNodes (though block values are rare for Ops)
            value_actual_str = str(self.value) if not isinstance(self.value, PdsNode) else self.value.to_string(0).strip()


        line_content = f"{indent_str}{self.key} {self.operator} {value_actual_str}"
        if self.comment_text_on_line:
            line_content += f" # {self.comment_text_on_line}"
        return line_content + "\n"

    def copy(self):
        new_value = self.value
        if isinstance(self.value, PdsNode): # Should be rare for Ops, but possible
            new_value = self.value.copy()

        new_node = PdsOperatorCondition(
            key=self.key,
            operator=self.operator,
            value=new_value,
            line_number=self.line_number,
            comment_text_on_line=self.comment_text_on_line
        )
        return self._base_copy_attrs(new_node)

    def get_structural_components(self, shallow_block=False): # shallow_block usually irrelevant
        value_comp = self.value
        if isinstance(self.value, PdsNode):
            value_comp = self.value.get_structural_components(shallow_block=shallow_block)
        # Primitives are compared directly
        return (self.__class__.__name__, self.key, self.operator, value_comp, self.comment_text_on_line)

class PdsBlock(PdsNode):
    def __init__(self, key, line_number=-1, comment_text_on_line=None): # key can be None for root anonymous blocks if supported
        super().__init__(line_number=line_number)
        self.key = key 
        self.children = [] # This list IS ORDERED as per file reading
        self.comment_text_on_line = comment_text_on_line # Comment on the line with "key = {"

    def add_child(self, node):
        if not isinstance(node, PdsNode):
            raise TypeError("Can only add PdsNode instances as children to PdsBlock")
        node.indent_level = self.indent_level + 4 # Default indent for children
        self.children.append(node)

    def to_string(self, current_indent=0, is_inline_context=False):
        indent_str = " " * current_indent
        
        # Try to render inline if simple and in inline context (e.g. value of a KVP)
        # Example: outer_key = { inner_key = { child_key = 1 } }
        # Here, "inner_key = { child_key = 1 }" might be the value of outer_key.
        # The PdsBlock for inner_key would be called with is_inline_context=True.
        # It might then try to render itself inline if its content (child_key=1) is simple enough.
        
        # Heuristic for inline: one simple KVP child, no block comment
        can_be_inline = (
            is_inline_context and
            len(self.children) == 1 and
            isinstance(self.children[0], PdsKeyValuePair) and
            not isinstance(self.children[0].value, PdsBlock) and # child KVP's value is not another block
            not self.comment_text_on_line # Block itself has no opening line comment
        )

        if can_be_inline:
            # Render child with zero indent relative to current block's context, then strip.
            child_str_compact = self.children[0].to_string(current_indent=0, is_inline_context_ignored=True).strip()
            block_key_prefix = f"{self.key} = " if self.key is not None else "" # Anonymous blocks just start with {
            return f"{block_key_prefix}{{{child_str_compact}}}" # No newline for inline block value

        # Multi-line rendering
        output_lines = []
        block_key_prefix = f"{self.key} = " if self.key is not None else ""
        open_brace_line_content = f"{indent_str}{block_key_prefix}{{"
        if self.comment_text_on_line: 
            open_brace_line_content += f" # {self.comment_text_on_line}" 
        output_lines.append(open_brace_line_content + "\n")

        children_expected_indent = current_indent + 4
        for child in self.children:
            child.indent_level = children_expected_indent # Ensure child indent is correct
            output_lines.append(child.to_string(children_expected_indent)) 
        
        output_lines.append(f"{indent_str}}}\n") 
        return "".join(output_lines)
    
    def _parse_indexed_identifier(self, identifier_full):
        """Helper to parse 'key___index' or just 'key'."""
        if isinstance(identifier_full, str) and "___" in identifier_full:
            parts = identifier_full.split("___", 1)
            base_key = parts[0]
            try:
                index = int(parts[1])
                return base_key, index, True # is_indexed
            except ValueError: # "___" was part of the key itself
                return identifier_full, 0, False # Treat as non-indexed key
        return str(identifier_full) if identifier_full is not None else None, 0, False # Non-indexed

    def find_child_by_key(self, key_name, Nth=0):
        """Finds the Nth child node with the given key_name. Nth is 0-indexed."""
        count = 0
        for child in self.children:
            if hasattr(child, 'key') and child.key == key_name:
                if count == Nth:
                    return child
                count += 1
        return None
        
    def find_all_children_by_key(self, key_name):
        """Returns a list of all child nodes with the given key_name."""
        return [child for child in self.children if hasattr(child, 'key') and child.key == key_name]

    def copy(self):
        new_node = PdsBlock(
            key=self.key,
            line_number=self.line_number,
            comment_text_on_line=self.comment_text_on_line
        )
        self._base_copy_attrs(new_node) # Applies indent_level correctly
        new_node.children = [child.copy() for child in self.children] 
        return new_node

    def replace_child(self, old_child_node_instance, new_child_node):
        """Replaces a specific child node instance with a new node."""
        if not isinstance(new_child_node, PdsNode):
            raise TypeError("New child must be a PdsNode instance.")
        try:
            idx = self.children.index(old_child_node_instance)
            new_child_node.indent_level = old_child_node_instance.indent_level # Preserve indent
            self.children[idx] = new_child_node
            return True
        except ValueError:
            return False # old_child_node_instance not found

    def remove_child(self, child_node_instance):
        """Removes a specific child node instance."""
        try:
            self.children.remove(child_node_instance)
            return True
        except ValueError:
            return False # child_node_instance not found

    def add_child_at_appropriate_location(self, new_child_node, after_node_instance=None, before_node_instance=None):
        """
        Adds a new child.
        - If before_node_instance is given, adds before it.
        - Else if after_node_instance is given, adds after it.
        - Else, adds to the end (common for new items).
        """
        if not isinstance(new_child_node, PdsNode):
            raise TypeError("New child must be a PdsNode instance.")
        new_child_node.indent_level = self.indent_level + 4 # Default indent

        if before_node_instance:
            try:
                idx = self.children.index(before_node_instance)
                self.children.insert(idx, new_child_node)
                return True
            except ValueError:
                pass # Fall through to append if before_node not found
        
        if after_node_instance:
            try:
                idx = self.children.index(after_node_instance)
                self.children.insert(idx + 1, new_child_node)
                return True
            except ValueError:
                pass # Fall through to append if after_node not found

        # Default: append (e.g., adding a new KVP to a character_modifier block)
        # Or, could try to find last non-comment/blank line and insert after.
        # For simplicity now, just append if no specific location is given.
        self.children.append(new_child_node)
        return True

    def get_structural_components(self, shallow_block=False):
        if shallow_block:
            # For SequenceMatcher comparing blocks as items in a list:
            # Key and opening line comment matter for "sameness" of the block item itself.
            return (self.__class__.__name__, self.key, self.comment_text_on_line)
        else:
            # For general structural equality (e.g., is block_A == block_B?):
            # Compare key, comment, AND children recursively.
            # The order of children in the list matters for exact structural equality of the AST.
            # The game might ignore order semantically, but our AST equality checks order.
            children_comps = tuple(c.get_structural_components(shallow_block=False) for c in self.children)
            return (self.__class__.__name__, self.key, self.comment_text_on_line, children_comps)

class PdsParser:
    def __init__(self):
        self.tokens = []
        self.current_token_index = 0
        self.root_nodes = [] # Top-level nodes in the file
        self.parent_stack = [] # Stack of PdsBlock nodes currently being parsed

    def _error(self, message, token_override=None):
        token_info = ""
        token_to_report = token_override if token_override else self._peek()
        
        if token_to_report and token_to_report.type != 'EOF':
            token_info = f"at token '{token_to_report.value}' (type: {token_to_report.type}) on line {token_to_report.line}, column {token_to_report.column}"
        elif self.current_token_index > 0 and self.current_token_index <= len(self.tokens):
            prev_token = self.tokens[self.current_token_index -1] # Use the token before current EOF
            token_info = f"after token '{prev_token.value}' (type: {prev_token.type}) on line {prev_token.line}, column {prev_token.column}"
        else:
            token_info = "at beginning of file or empty file"
            
        raise ValueError(f"Parser Error: {message}. {token_info}")

    def _peek(self, offset=0):
        idx = self.current_token_index + offset
        if idx < len(self.tokens):
            return self.tokens[idx]
        # Should always have an EOF token at the end from lexer
        return self.tokens[-1] if self.tokens else PdsToken('EOF', '', 1, 1)


    def _advance(self):
        if self.current_token_index < len(self.tokens) -1: # Stop at EOF
             self.current_token_index += 1
        return self._peek(-1) # Return the consumed token

    def _consume(self, *expected_types):
        token = self._peek()
        if token.type in expected_types:
            return self._advance()
        self._error(f"Expected one of {expected_types}", token_override=token)

    def _is_next(self, *token_types):
        return self._peek().type in token_types

    def is_eof(self):
        return self._peek().type == 'EOF'

    def _parse_comment_node(self):
        # Assumes current token is COMMENT
        token = self._consume('COMMENT')
        return PdsComment(token.value, token.line) # Value is text after #

    def _parse_blank_line_node(self):
        # Consumes one or more NEWLINE tokens that form blank lines
        first_newline_token = self._consume('NEWLINE') 
        line_number = first_newline_token.line
        # Keep consuming subsequent NEWLINEs on their own lines
        while self._is_next('NEWLINE') and self._peek(1).type != 'EOF' and \
              self._peek(0).line != self._peek(1).line : # Ensure it's truly a new line vs. end of statement
            # This logic is tricky, just consume one for now if it's for an empty line statement
            break # Let main loop handle multiple blank lines
        return PdsBlankLine(line_number)
        
    def _parse_primitive_value(self):
        token = self._peek()
        if token.type == 'IDENTIFIER':
            self._advance()
            if token.value.lower() == "yes": return True
            if token.value.lower() == "no": return False
            return token.value
        elif token.type == 'NUMBER':
            self._advance()
            try: return float(token.value) if '.' in token.value else int(token.value)
            except ValueError: return token.value # Should not happen if lexer is correct
        elif token.type == 'STRING':
            self_consuming_token = self._advance() # Consume the STRING token
            # Unescape string content: remove outer quotes and handle \", \\
            val_str = self_consuming_token.value[1:-1] # Remove quotes
            val_str = val_str.replace('\\"', '"').replace('\\\\', '\\')
            return val_str
        return None # Not a primitive

    def _parse_value_for_kvp_or_op(self):
        # A value can be a primitive or a block
        if self._is_next('LBRACE'): # Value is a block: key = { ... }
            # The LBRACE is for an anonymous block as a value
            # Example: effect = { limit = { always = yes } }
            # Here, the value for `effect` is an anonymous block.
            # The value for `limit` is also an anonymous block.
            # The key for these blocks is None.
            return self._parse_block_content(key_for_block=None, line_of_key=self._peek().line)
        else:
            primitive_val = self._parse_primitive_value()
            if primitive_val is not None:
                return primitive_val
            else:
                self._error(f"Expected a value (primitive or block {{...}})")
        return None


    def _parse_statement_or_item(self):
        # This can be a KVP, an OperatorCondition, or a bare value (for lists)
        # This is called from within a block or at the root.
        
        # Skip leading newlines within a block before a statement
        while self._is_next('NEWLINE'):
            self._advance()

        start_token = self._peek()
        
        if start_token.type == 'COMMENT':
            return self._parse_comment_node()
        # Blank lines are tricky here, usually handled by main loop or block loop consuming newlines.
        # If a NEWLINE is explicitly a PdsBlankLine node, it should be added.
        # For now, statements don't start with NEWLINE after skipping.

        if not (start_token.type == 'IDENTIFIER' or start_token.type == 'NUMBER' or start_token.type == 'STRING'):
            # This could be an anonymous block in a list context, e.g. list = { { nested_key=val } }
            # Or a bare value in a list.
             if self._is_next('LBRACE'): # Anonymous block in list or bare block
                return self._parse_block_content(key_for_block=None, line_of_key=start_token.line)
             
             primitive = self._parse_primitive_value() # For bare values in a PdsList
             if primitive is not None: return primitive # Return the primitive directly for PdsList
             
             self._error(f"Statement or list item must start with IDENTIFIER, NUMBER, STRING, or LBRACE for anonymous block")

        # At this point, start_token is IDENTIFIER, NUMBER, or STRING (potential key)
        key_candidate_token = self._advance() # Consume the key/identifier
        key_str = key_candidate_token.value
        key_line = key_candidate_token.line

        # Handle string keys (remove quotes)
        if key_candidate_token.type == 'STRING':
            key_str = key_str[1:-1].replace('\\"', '"').replace('\\\\', '\\')


        # Look ahead for operator or equals
        if self._is_next('EQUALS'):
            self._consume('EQUALS')
            # Now expect value: primitive, block { ... }, or list { v1 v2 }
            
            # To distinguish block "key = { k2=v2 }" from list "key = { v1 v2 }"
            # we need to look inside the LBRACE.
            # If next is LBRACE:
            if self._is_next('LBRACE'):
                lbrace_token_for_value = self._peek() # Don't consume yet
                
                # Heuristic: Look at token *after* LBRACE.
                # If IDENTIFIER then EQUALS, it's likely a block.
                # Otherwise, it's likely a list of values.
                # This needs a more robust lookahead or trial parsing.
                
                # Simple heuristic: if token after LBRACE is IDENTIFIER and token after that is EQUALS/OPERATOR/LBRACE, it's a block.
                # Otherwise, it's a list. This is imperfect for mixed lists/objects.
                # For now, assume key = { ... } always means PdsBlock if followed by KVP-like structures
                # and PdsList if followed by simple values.
                
                # Let _parse_block_or_list_after_equals handle this.
                return self._parse_block_or_list_after_equals(key_str, key_line)

            else: # Value is a primitive
                value_node = self._parse_primitive_value()
                if value_node is None: self._error("Expected primitive value after '='")
                
                comment_on_line = None
                if self._is_next('COMMENT') and self._peek().line == self.tokens[self.current_token_index-1].line: # Comment on same line as value
                    comment_on_line = self._consume('COMMENT').value
                return PdsKeyValuePair(key_str, value_node, key_line, comment_on_line)

        elif self._is_next('OPERATOR'):
            op_token = self._consume('OPERATOR')
            value_node = self._parse_value_for_kvp_or_op() # RHS of operator
            comment_on_line = None
            if self._is_next('COMMENT') and self._peek().line == self.tokens[self.current_token_index-1].line:
                 comment_on_line = self._consume('COMMENT').value
            return PdsOperatorCondition(key_str, op_token.value, value_node, key_line, comment_on_line)
        
        elif self._is_next('LBRACE'): # Key followed by LBRACE implies a named block: key { ... }
            # This syntax "key { ... }" is less common than "key = { ... }" in many PDS files
            # but is valid. The key_str is the block's key.
            return self._parse_block_content(key_str, key_line)
        else:
            # If it was an IDENTIFIER/NUMBER/STRING not followed by =, operator, or {,
            # it must be a bare value if we are in a PdsList parsing context.
            # This function is general, so it indicates an error if not in such context.
            # The caller (_parse_block_content) will decide if this is a list item.
            # Here, we return the consumed key_candidate_token's value as a potential list item.
            if key_candidate_token.type == 'IDENTIFIER' and key_str.lower() == "yes": return True
            if key_candidate_token.type == 'IDENTIFIER' and key_str.lower() == "no": return False
            if key_candidate_token.type == 'NUMBER':
                try: return float(key_str) if '.' in key_str else int(key_str)
                except ValueError: return key_str
            return key_str # Return raw identifier/string value

    def _parse_block_or_list_after_equals(self, key_str, key_line):
        # We have "key = " and next token is LBRACE. Consume LBRACE.
        lbrace_token = self._consume('LBRACE')
        comment_on_lbrace_line = None
        if self._is_next('COMMENT') and self._peek().line == lbrace_token.line:
            comment_on_lbrace_line = self._consume('COMMENT').value

        # Look ahead to distinguish PdsBlock from PdsList
        # If the first non-comment/newline token inside { is IDENTIFIER and *then* EQUALS/OPERATOR/LBRACE, it's a block.
        # Otherwise, it's a list of values. This is still a heuristic.
        # A more robust parser might try parsing as block, if fails, try as list.
        is_likely_block = False
        temp_idx = self.current_token_index
        while temp_idx < len(self.tokens):
            peek_token = self.tokens[temp_idx]
            if peek_token.type == 'COMMENT' or peek_token.type == 'NEWLINE':
                temp_idx += 1
                continue
            if peek_token.type == 'RBRACE': # Empty or only comments/newlines
                break 
            if peek_token.type == 'IDENTIFIER' or peek_token.type == 'STRING' or peek_token.type == 'NUMBER':
                # Check token *after* this potential key
                if temp_idx + 1 < len(self.tokens):
                    nextToken = self.tokens[temp_idx+1]
                    if nextToken.type == 'EQUALS' or nextToken.type == 'OPERATOR' or nextToken.type == 'LBRACE':
                        is_likely_block = True
                break # Found first significant token
            break # Not a key-like token, so must be a list of values (or error)

        if is_likely_block:
            # It's a PdsBlock: key = { child_key = child_value ... }
            # The key_str is the block's key.
            block_node = self._parse_block_content(key_str, key_line, is_named_block_rhs=True)
            if comment_on_lbrace_line: # This comment belongs to the block itself
                block_node.comment_text_on_line = comment_on_lbrace_line
            return block_node
        else:
            # It's a PdsList: key = { val1 val2 "val 3" }
            list_node = PdsList(key_str, [], key_line, comment_on_lbrace_line)
            while not self._is_next('RBRACE') and not self.is_eof():
                # Skip newlines and comments between list items
                while self._is_next('NEWLINE') or self._is_next('COMMENT'):
                    if self._is_next('COMMENT'): # Add comments as siblings if they are significant? No, usually ignore in lists.
                         self._advance() # Consume comments within list values area
                    else:
                         self._advance() # Consume newlines
                    if self.is_eof(): break
                if self._is_next('RBRACE') or self.is_eof(): break

                list_item_value = self._parse_statement_or_item() # Gets primitive or anonymous block
                if list_item_value is None and not self._is_next('RBRACE'): # Should not happen if parsing is correct
                    self._error("Expected value or RBRACE in list")
                if list_item_value is not None: # Can be None if only comments/newlines left
                    list_node.values.append(list_item_value)
            
            self._consume('RBRACE') # Consume the closing brace of the list
            # Any comment after RBRACE on same line? PdsList handles this in constructor.
            # If comment_on_lbrace_line was for key = { #comment ...}, PdsList stores it.
            return list_node


    def _parse_block_content(self, key_for_block, line_of_key, is_named_block_rhs=False):
        # Assumes LBRACE was just consumed if is_named_block_rhs or if it's an anonymous block value.
        # If it's a "key { ... }" style block, LBRACE is next and needs to be consumed.
        
        comment_on_opening_brace_line = None
        if not is_named_block_rhs : # If "key {", LBRACE is next
            lbrace_token = self._consume('LBRACE')
            if self._is_next('COMMENT') and self._peek().line == lbrace_token.line:
                 comment_on_opening_brace_line = self._consume('COMMENT').value
        else: # If "key = {", LBRACE was already consumed by caller.
              # The comment_on_lbrace_line was also handled by caller and passed if PdsBlock needs it.
              # Here, key_for_block and line_of_key refer to the 'key ='.
              # The comment is associated with the block itself.
              pass


        block_node = PdsBlock(key_for_block, line_of_key, comment_on_opening_brace_line)
        
        current_parent_indent = self.parent_stack[-1].indent_level if self.parent_stack else -4
        block_node.indent_level = current_parent_indent + 4

        self.parent_stack.append(block_node)

        while not self._is_next('RBRACE') and not self.is_eof():
            # Handle newlines and comments first
            if self._is_next('NEWLINE'):
                # Check for multiple newlines to form a PdsBlankLine
                # A PdsBlankLine is a node that represents one or more consecutive blank lines.
                start_line = self._peek().line
                self._advance() # Consume first NEWLINE
                is_true_blank_line = True
                # Consume subsequent newlines if they are on different lines
                while self._is_next('NEWLINE'):
                    if self._peek().line == start_line: # Still on same line, not a blank line node
                        is_true_blank_line = False; break 
                    self._advance()
                    start_line = self._peek(-1).line # update start_line to previous token's line
                if is_true_blank_line:
                     # If last token was NEWLINE and next is not RBRACE or EOF, it's a blank line node
                     # This logic for blank lines needs refinement. A blank line is NEWLINE not followed by content on same line.
                     # And then another NEWLINE.
                     # Simpler: if consecutive newlines, first one is blank line node.
                     block_node.add_child(PdsBlankLine(self._peek(-1).line))
                continue # Re-evaluate next token
            
            if self._is_next('COMMENT'):
                block_node.add_child(self._parse_comment_node())
                continue

            if self.is_eof(): self._error("Unexpected EOF inside block", token_override=self._peek(-1))

            # Now parse a statement (KVP, OperatorCondition, or nested Block)
            child_node = self._parse_statement_or_item()
            if child_node is not None: # _parse_statement_or_item can return None if only comments consumed
                 if not isinstance(child_node, PdsNode):
                     # This means _parse_statement_or_item returned a primitive,
                     # which implies this block is being parsed as a PdsList implicitly.
                     # This indicates a parser logic error: a block should not contain raw primitives.
                     self._error(f"Block '{key_for_block}' cannot directly contain primitive value '{child_node}'. Possible PdsList misidentified as PdsBlock.")
                 block_node.add_child(child_node)
            elif not self._is_next('RBRACE') and not self.is_eof(): # No progress
                 # This case should be rare if lexer/parser handles all tokens.
                 # Could be an issue with unexpected token type not caught above.
                 self._error(f"Parser stuck in block '{key_for_block}' before token", token_override=self._peek())


        self._consume('RBRACE')
        self.parent_stack.pop()
        return block_node

    def parse_file(self, filepath):
        self.root_nodes = [] 
        self.parent_stack = []
        self.current_token_index = 0

        if not os.path.exists(filepath):
            sys.stderr.write(f"Error: File not found: {filepath}\n"); return [] 
        try:
            with open(filepath, 'r', encoding='utf-8-sig') as f: text_content = f.read()
        except UnicodeDecodeError:
            try: 
                with open(filepath, 'r', encoding='utf-8') as f: text_content = f.read()
            except Exception as e_inner:
                sys.stderr.write(f"Error reading {filepath} (fallback UTF-8): {e_inner}\n"); return []
        except Exception as e:
            sys.stderr.write(f"Error reading {filepath}: {e}\n"); return []
        
        lexer = PdsLexer(text_content)
        try:
            self.tokens = lexer.tokenize()
        except ValueError as lex_err:
            sys.stderr.write(f"Lexer error in {filepath}: {lex_err}\n"); return []

        if not self.tokens or self.tokens[0].type == 'EOF': return [] # Empty or only EOF
            
        # Main parsing loop for root-level statements
        while not self.is_eof():
            # Skip newlines and comments at root level before a statement
            if self._is_next('NEWLINE'):
                # Check if it's a PdsBlankLine node vs just whitespace between statements
                # A blank line node is for preserving visual structure if desired.
                # Here, we are interested in structured nodes.
                # Let's see if multiple NEWLINEs constitute a PdsBlankLine node.
                start_line = self._peek().line
                self._advance() # consume first NEWLINE
                if self._is_next('NEWLINE') and self._peek().line != start_line: # Truly a blank line if next newline is on different line
                    self.root_nodes.append(PdsBlankLine(start_line))
                # If not a blank line node, the newline is just consumed whitespace.
                continue 
            
            if self._is_next('COMMENT'):
                self.root_nodes.append(self._parse_comment_node())
                continue
            
            if self.is_eof(): break # Check again after consuming comments/newlines

            try:
                # Root level items are typically Key-Value (key = {block} or key = value)
                # or Key-Operator (key > value)
                node = self._parse_statement_or_item() # This should yield KVP, OpCond, or Block
                if node:
                    if not isinstance(node, (PdsKeyValuePair, PdsOperatorCondition, PdsBlock, PdsList, PdsComment, PdsBlankLine)):
                        self._error(f"Root level statement parsed into unexpected type: {type(node)}. Value: {node}")
                    self.root_nodes.append(node)
                elif not self.is_eof(): # No node parsed, but not EOF
                    self._error(f"Parser did not produce a node and is not at EOF.")
            except ValueError as parse_err:
                sys.stderr.write(f"Parser error in {filepath}: {parse_err}\n")
                # Optionally, decide if parsing should stop or try to recover (not implemented)
                return self.root_nodes # Return what was parsed so far
        return self.root_nodes

    @staticmethod
    def _nodes_to_string(nodes_list):
        output = []
        for node in nodes_list:
            # Root nodes typically have indent 0, PdsBlock.to_string handles children.
            # Make sure indent_level is set if None (should be by parser)
            node_indent = node.indent_level if node.indent_level is not None else 0
            output.append(node.to_string(node_indent)) 
        return "".join(output)

    def to_string(self): # For the whole parsed file
        return PdsParser._nodes_to_string(self.root_nodes)