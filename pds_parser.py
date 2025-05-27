import re
import os
import difflib

# --- PdsLexer and PdsToken ---

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
        
        self.patterns = [
            # COMMENT: Capture everything after the #, including leading/trailing whitespace
            ('COMMENT', r'#.*'), 
            ('OPERATOR', r'(?:>=|<=|==|!=|\?=|>|<|!)'), 
            ('STRING', r'"[^"]*"'), 
            ('NUMBER', r'-?\d+(?:\.\d+)?'),
            ('IDENTIFIER', r'[\w\.:@\-]+(?:[\w\.:@\-]*[\w\.:@\-])?'), 
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
                whitespace_match = re.match(r'\s+', self.text[self.pos:])
                if whitespace_match:
                    whitespace_block = whitespace_match.group(0)
                    for ws_char in whitespace_block:
                        if ws_char == '\n':
                            self._add_token_and_advance('NEWLINE', '\n', 1)
                        else:
                            self._advance(1) 
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
                        # Preserve original leading space after '#'
                        self._add_token_and_advance(token_type, token_value_raw[1:], len(token_value_raw))
                    else:
                        self._add_token_and_advance(token_type, token_value_raw, len(token_value_raw))
                    matched = True
                    break 

            if matched:
                continue 

            raise ValueError(f"Lexer Error: Unexpected character '{char}' at line {self.line}, column {error_reporting_column}")
        
        self.tokens.append(PdsToken('EOF', '', self.line, self.column)) 
        return self.tokens

# --- PdsNode Classes (Major changes to PdsComment and PdsBlock to_string) ---

class PdsNode:
    def __init__(self, line_number=-1): 
        self.line_number = line_number
        self.indent_level = 0 
        self.comment_text_on_line = None

    def to_string(self, current_indent=0, is_inline_context=False): # Added is_inline_context
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
        """Helper to copy common attributes (called AFTER specific __init__)."""
        new_node.indent_level = self.indent_level
        new_node.comment_text_on_line = self.comment_text_on_line
        return new_node

    def copy(self): 
        # This method MUST be overridden by concrete subclasses to ensure correct __init__
        raise NotImplementedError(f"copy() not implemented for {self.__class__.__name__}")


class PdsComment(PdsNode):
    def __init__(self, comment_text, line_number=-1):
        super().__init__(line_number=line_number)
        self.comment_text = comment_text

    def to_string(self, current_indent=0, is_inline_context=False): # is_inline_context not used here
        indent_str = " " * current_indent
        # No hardcoded space after '#' - rely on lexer to preserve it if present
        return f"{indent_str}#{self.comment_text}\n"

    def copy(self):
        new_node = PdsComment(comment_text=self.comment_text, line_number=self.line_number)
        return self._base_copy_attrs(new_node)

class PdsBlankLine(PdsNode):
    def __init__(self, line_number=-1):
        super().__init__(line_number=line_number)

    def to_string(self, current_indent=0, is_inline_context=False): # is_inline_context not used here
        return "\n" 

    def copy(self):
        new_node = PdsBlankLine(line_number=self.line_number)
        return self._base_copy_attrs(new_node)


class PdsKeyValuePair(PdsNode):
    _lexer_identifier_pattern = r'[\w\.:@\-]+' 

    def __init__(self, key, value, line_number=-1, comment_text=None):
        super().__init__(line_number=line_number)
        self.key = key
        self.value = value 
        self.comment_text_on_line = comment_text

    def to_string(self, current_indent=0, is_inline_context_ignored=False): # Renamed param
        indent_str = " " * current_indent
        line_content_start = f"{indent_str}{self.key} = "
        value_actual_str = ""
        line_ending = "\n"

        if isinstance(self.value, PdsBlock):
            block_node = self.value
            # PdsBlock.to_string will use current_indent for its own structure if multi-line,
            # or return a compact string like "{{ child }}" if it's an inline candidate.
            # The is_inline_context=True tells the block it's being used as a value.
            block_render_str = block_node.to_string(current_indent, is_inline_context=True)

            if block_render_str.strip().startswith("{") and not "\n" in block_render_str:
                # Compact inline block, e.g., "{{ child_content }}"
                value_actual_str = block_render_str.strip() # -> "{child_content}"
                # line_content_start already has "key = "
            else:
                # Multi-line block. block_render_str is like "    {\n      child\n    }\n"
                # It already starts with the correct current_indent.
                # We want to avoid "key =     {...}", so remove the redundant indent from block_render_str
                if block_render_str.startswith(indent_str):
                    value_actual_str = block_render_str[len(indent_str):]
                else: # Should not happen if block is multi-line and correctly indented
                    value_actual_str = block_render_str
                line_ending = "" # Block's string has its own newlines
        elif isinstance(self.value, str):
            # ... (your existing string quoting logic) ...
            must_quote = (' ' in self.value or '\t' in self.value or '#' in self.value or '=' in self.value or '{' in self.value or '}' in self.value or '"' in self.value or not self.value)
            is_simple_identifier = re.fullmatch(PdsKeyValuePair._lexer_identifier_pattern, self.value)
            if must_quote or (self.value and not is_simple_identifier): value_actual_str = f'"{self.value}"'
            else: value_actual_str = self.value
        else: 
            value_actual_str = str(self.value)
        
        line_content = line_content_start + value_actual_str.lstrip() # lstrip to remove any leading space from value if it's "{...}"
        if self.comment_text_on_line: 
            line_content += f" # {self.comment_text_on_line}"
        
        return line_content + line_ending


    def copy(self):
        new_node = PdsKeyValuePair(
            key=self.key, 
            value=self.value.copy() if isinstance(self.value, PdsNode) else self.value,
            line_number=self.line_number,
            comment_text=self.comment_text_on_line
        )
        return self._base_copy_attrs(new_node)

class PdsList(PdsNode): 
    def __init__(self, key, values, line_number=-1, comment_text=None): 
        super().__init__(line_number=line_number)
        self.key = key
        self.values = values 
        self.comment_text_on_line = comment_text

    def to_string(self, current_indent=0, is_inline_context=False): # is_inline_context not directly used here
        indent_str = " " * current_indent
        formatted_values = []
        for v in self.values:
            if isinstance(v, str):
                if ' ' in v or '\t' in v or not v or \
                   not re.fullmatch(PdsKeyValuePair._lexer_identifier_pattern, v): 
                    formatted_values.append(f'"{v}"')
                else:
                    formatted_values.append(v)
            else:
                formatted_values.append(str(v))

        value_str = " ".join(formatted_values) 
        inner_content = f" {value_str} " if self.values else " " 
        base_string = f"{indent_str}{self.key} = {{{inner_content}}}"
        if self.comment_text_on_line:
            return f"{base_string} # {self.comment_text_on_line}\n"
        return f"{base_string}\n"

    def copy(self):
        new_node = PdsList(
            key=self.key,
            values=list(self.values),
            line_number=self.line_number,
            comment_text=self.comment_text_on_line
        )
        return self._base_copy_attrs(new_node)

class PdsOperatorCondition(PdsNode):
    def __init__(self, key, operator, value, line_number=-1, comment_text=None):
        super().__init__(line_number=line_number)
        self.key = key
        self.operator = operator 
        self.value = value        
        self.comment_text_on_line = comment_text

    def to_string(self, current_indent=0, is_inline_context_ignored=False): # Renamed param
        indent_str = " " * current_indent
        line_content_start = f"{indent_str}{self.key} {self.operator} "
        value_actual_str = ""
        line_ending = "\n"

        if isinstance(self.value, PdsBlock):
            block_node = self.value
            block_render_str = block_node.to_string(current_indent, is_inline_context=True)

            if block_render_str.strip().startswith("{") and not "\n" in block_render_str:
                value_actual_str = block_render_str.strip()
            else:
                if block_render_str.startswith(indent_str):
                    value_actual_str = block_render_str[len(indent_str):]
                else:
                    value_actual_str = block_render_str
                line_ending = ""
        elif isinstance(self.value, str):
            # ... (your existing string quoting logic) ...
            must_quote = (' ' in self.value or '\t' in self.value or '#' in self.value or '=' in self.value or '{' in self.value or '}' in self.value or '"' in self.value or not self.value)
            is_simple_identifier = re.fullmatch(PdsKeyValuePair._lexer_identifier_pattern, self.value) # Assuming pattern is accessible
            if must_quote or (self.value and not is_simple_identifier): value_actual_str = f'"{self.value}"'
            else: value_actual_str = self.value
        else: 
            value_actual_str = str(self.value)
        
        line_content = line_content_start + value_actual_str.lstrip() # lstrip to remove any leading space from value if it's "{...}"
        if self.comment_text_on_line:
            line_content += f" # {self.comment_text_on_line}"
        
        return line_content + line_ending


    def copy(self):
        new_node = PdsOperatorCondition(
            key=self.key,
            operator=self.operator,
            value=self.value.copy() if isinstance(self.value, PdsNode) else self.value,
            line_number=self.line_number,
            comment_text=self.comment_text_on_line
        )
        return self._base_copy_attrs(new_node)

class PdsBlock(PdsNode): # Ensure these methods are part of PdsBlock
    def __init__(self, key, line_number=-1, comment_text=None):
        super().__init__(line_number=line_number)
        self.key = key 
        self.children = []
        self.comment_text_on_line = comment_text

    def add_child(self, node):
        node.indent_level = self.indent_level + 4 # Standard PDS indent
        self.children.append(node)

    def to_string(self, current_indent=0, is_inline_context=False): # Your provided version
        indent_str = " " * current_indent
        
        is_inline_candidate = (
            is_inline_context and 
            len(self.children) == 1 and
            isinstance(self.children[0], (PdsKeyValuePair, PdsOperatorCondition)) and
            not isinstance(self.children[0].value, PdsBlock) and 
            not self.comment_text_on_line 
        )

        if is_inline_candidate:
            child_content_compact = self.children[0].to_string(0).strip() 
            key_part_for_block = f"{self.key} " if self.key else "" 
            return f"{key_part_for_block}{{{child_content_compact}}}" # For anonymous, this is "{{child_compact}}"
        
        output_lines = []
        open_brace_line_content = ""
        if self.key: 
            open_brace_line_content = f"{self.key} {{"
        else: 
            open_brace_line_content = "{"

        if self.comment_text_on_line: 
            open_brace_line_content += f" # {self.comment_text_on_line}" 
        
        output_lines.append(indent_str + open_brace_line_content + "\n")

        # Children's indent_level is set by add_child relative to this block's indent_level.
        # So, they should use their own self.indent_level for rendering.
        # The current_indent here is for THIS block's braces.
        children_expected_indent = current_indent + 4
        for child in self.children:
            # If child.indent_level is correctly set, child.to_string(child.indent_level) is fine.
            # For safety, or if indent_levels might be stale after tree manipulation not using add_child:
            output_lines.append(child.to_string(children_expected_indent)) 
        
        output_lines.append(f"{indent_str}}}\n") 
        return "".join(output_lines)
    
    def find_node(self, key_path): # Your provided version
        if isinstance(key_path, str): key_path = key_path.split('.')
        if not key_path: return None
        current_nodes_to_search = self.children
        for i, segment in enumerate(key_path):
            found_node_for_segment = None
            for node in current_nodes_to_search:
                node_key_str = str(node.key) if hasattr(node, 'key') else None
                if node_key_str == segment: 
                    if i == len(key_path) - 1: return node 
                    elif isinstance(node, PdsBlock): 
                        found_node_for_segment = node; break 
                    else: return None 
            if found_node_for_segment: current_nodes_to_search = found_node_for_segment.children
            else: return None 
        return None 

    def copy(self): # Your provided version
        new_node = PdsBlock(
            key=self.key,
            line_number=self.line_number,
            comment_text=self.comment_text_on_line
        )
        new_node = self._base_copy_attrs(new_node)
        new_node.children = [child.copy() for child in self.children] 
        return new_node

    # MODIFIED/ENSURED METHODS for indent_level during merge
    def replace_child(self, old_child_identifier, new_child_node):
        new_child_node.indent_level = self.indent_level + 4 # Ensure new child gets correct indent
        for i, child in enumerate(self.children):
            child_key_str = str(child.key) if hasattr(child, 'key') else None
            if child_key_str == str(old_child_identifier): 
                self.children[i] = new_child_node
                return True
        return False

    def remove_child(self, child_identifier): # Your provided version
        original_len = len(self.children)
        self.children = [c for c in self.children if not (hasattr(c, 'key') and str(c.key) == str(child_identifier))]
        return len(self.children) < original_len

    def add_child_at_appropriate_location(self, new_child_node, target_sibling_identifier=None, after=True):
        new_child_node.indent_level = self.indent_level + 4 # Ensure new child gets correct indent
        
        if target_sibling_identifier:
            for i, child in enumerate(self.children):
                child_key_str = str(child.key) if hasattr(child, 'key') else None
                if child_key_str == str(target_sibling_identifier): 
                    self.children.insert(i + 1 if after else i, new_child_node)
                    return True
        
        last_code_node_idx = -1
        for i in reversed(range(len(self.children))):
            if not isinstance(self.children[i], (PdsComment, PdsBlankLine)):
                last_code_node_idx = i
                break
        if last_code_node_idx != -1: 
            self.children.insert(last_code_node_idx + 1, new_child_node)
        else: 
            self.children.append(new_child_node)
        return True

# --- PdsParser (No changes needed from previous iteration) ---
class PdsParser:
    def __init__(self):
        self.tokens = []
        self.current_token_index = 0
        self.root_nodes = []
        self.stack = [] 

    def _error(self, message):
        token_info = ""
        current_processing_token = self._peek() 
        if current_processing_token and current_processing_token.type != 'EOF':
            token_info = f"at '{current_processing_token.value}' (type: {current_processing_token.type}) on line {current_processing_token.line}, column {current_processing_token.column}"
        elif self.current_token_index > 0 and self.current_token_index <= len(self.tokens):
            prev_token = self.tokens[self.current_token_index -1]
            token_info = f"after '{prev_token.value}' (type: {prev_token.type}) on line {prev_token.line}, column {prev_token.column}"
        raise ValueError(f"Parser Error: {message} {token_info}")

    def _peek(self, offset=0):
        idx = self.current_token_index + offset
        if idx < len(self.tokens):
            return self.tokens[idx]
        last_line = 1
        if self.tokens:
            # If last token was EOF, use the line of the token before it for a more useful EOF location.
            if self.tokens[-1].type == 'EOF' and len(self.tokens) > 1:
                last_line = self.tokens[-2].line
            else: # Use the last token's line
                last_line = self.tokens[-1].line
        return PdsToken('EOF', '', last_line, 1) # Default to column 1 for EOF

    def _advance(self):
        if self.current_token_index < len(self.tokens) -1: 
             self.current_token_index += 1

    def _consume(self, *expected_types):
        token = self._peek()
        if token.type in expected_types:
            self._advance()
            return token
        self._error(f"Expected one of {expected_types}, got {token.type}")

    def is_eof(self):
        return self._peek().type == 'EOF'

    def _parse_comment(self):
        token = self._consume('COMMENT')
        return PdsComment(token.value, token.line)

    def _parse_blank_line_node(self):
        """
        Parses and consumes all consecutive NEWLINE tokens that constitute a single logical blank line.
        Assumes the current token is a NEWLINE and it's confirmed to be part of a blank line sequence.
        """
        first_newline_token = self._consume('NEWLINE') 
        line_number = first_newline_token.line
        
        # Consume any additional consecutive NEWLINEs that form this single blank line logical unit
        while self._peek().type == 'NEWLINE':
            self._advance()
        
        return PdsBlankLine(line_number)

    def _parse_value(self): 
        value_token_peeked = self._peek()
        parsed_value = None
        
        if value_token_peeked.type == 'LBRACE':
            # Note: LBRACE token for anonymous block is handled in _parse_block directly
            # This branch for LBRACE is specifically when LBRACE is the *value* of a KVP/Operator
            lbrace_token = self._consume('LBRACE') 
            comment_on_lbrace_line = None
            if self._peek().type == 'COMMENT' and self._peek().line == lbrace_token.line:
                comment_on_lbrace_line = self._consume('COMMENT').value
            
            # Anonymous block: key is "", line number is lbrace_token.line
            parsed_value = self._parse_block("", lbrace_token.line) 
            if isinstance(parsed_value, PdsBlock) and comment_on_lbrace_line:
                parsed_value.comment_text_on_line = comment_on_lbrace_line
            return parsed_value

        elif value_token_peeked.type == 'IDENTIFIER':
            val_tok = self._consume('IDENTIFIER')
            parsed_value = val_tok.value 
        elif value_token_peeked.type == 'NUMBER':
            val_tok = self._consume('NUMBER')
            try: parsed_value = float(val_tok.value) if '.' in val_tok.value else int(val_tok.value)
            except ValueError: parsed_value = val_tok.value 
        elif value_token_peeked.type == 'STRING':
            val_tok = self._consume('STRING')
            parsed_value = val_tok.value[1:-1] 
        else:
            self._error(f"Expected a value (IDENTIFIER, NUMBER, STRING, or LBRACE for a block) but got {value_token_peeked.type}")
            
        return parsed_value 


    def _parse_key_value_pair(self, key_string, key_line_number):
        # '=' is already consumed by the caller.
        value = self._parse_value() 
        
        comment_on_kvp_line = None
        # Only attach comment if value is not a block (blocks handle their own comments on LBRACE line)
        if not isinstance(value, PdsBlock) and self.current_token_index > 0:
            value_end_line = self.tokens[self.current_token_index-1].line
            if self._peek().type == 'COMMENT' and self._peek().line == value_end_line:
                 comment_on_kvp_line = self._consume('COMMENT').value
        
        return PdsKeyValuePair(key_string, value, key_line_number, comment_on_kvp_line)

    def _parse_operator_condition(self, key_string, operator_str, key_line_number):
        # Operator is already consumed by the caller.
        value = self._parse_value() 
        
        comment_on_op_line = None
        if not isinstance(value, PdsBlock) and self.current_token_index > 0:
            value_end_line = self.tokens[self.current_token_index-1].line
            if self._peek().type == 'COMMENT' and self._peek().line == value_end_line:
                 comment_on_op_line = self._consume('COMMENT').value

        return PdsOperatorCondition(key_string, operator_str, value, key_line_number, comment_on_op_line)

    def _parse_list(self, key_part_string, key_line_number): 
        values = []
        # LBRACE was consumed before calling this.
        
        while self._peek().type != 'RBRACE' and not self.is_eof():
            token = self._peek()
            if token.type in ['IDENTIFIER', 'NUMBER', 'STRING']:
                values.append(self._parse_value()) 
            elif token.type == 'NEWLINE' or token.type == 'COMMENT': 
                # Newlines and comments within a list's value section are just skipped as separators
                self._advance() 
            else:
                self._error(f"Unexpected token {token.type} inside PdsList values.")
        
        rbrace_token = self._consume('RBRACE') 
        comment_on_list_line = None
        # A comment for a list usually appears on the line of `key = { ... # comment }`
        # Check if comment is on same line as RBRACE.
        if self._peek().type == 'COMMENT' and self._peek().line == rbrace_token.line: 
            comment_on_list_line = self._consume('COMMENT').value
            
        return PdsList(key_part_string, values, key_line_number, comment_on_list_line) 

    def _parse_block(self, key_part_string, key_line_number_of_key): 
        # LBRACE was consumed just before this call.
        lbrace_token_just_consumed = self.tokens[self.current_token_index -1]
        
        # Comment for the block (on its LBRACE line)
        comment_for_block_opening = None
        if self._peek().type == 'COMMENT' and self._peek().line == lbrace_token_just_consumed.line:
            comment_for_block_opening = self._consume('COMMENT').value

        block_node = PdsBlock(key_part_string, key_line_number_of_key, comment_for_block_opening) 
        self.stack.append(block_node)

        # Set indent level for the block node itself.
        if not self.stack or len(self.stack) == 1: # This is a root block
             block_node.indent_level = 0
        elif len(self.stack) > 1: # Nested block
             parent_block = self.stack[-2] # The block that contains this new one
             block_node.indent_level = parent_block.indent_level + 4


        # Main loop for parsing children within a block
        while self._peek().type != 'RBRACE' and not self.is_eof():
            token = self._peek()
            
            if token.type == 'NEWLINE':
                # A PdsBlankLine node is created only for sequences of two or more newlines.
                # A single newline is simply consumed as a separator.
                if self._peek(1).type == 'NEWLINE': # Check for double newline
                    block_node.add_child(self._parse_blank_line_node())
                else: # Single newline, just consume as separator
                    self._advance()
            elif token.type == 'COMMENT':
                block_node.add_child(self._parse_comment())
            elif token.type == 'EOF':
                self._error("Unexpected EOF inside block.")
            else: # Must be a statement (KVP, Operator, List, Block)
                child_node = self._parse_statement()
                if child_node:
                    block_node.add_child(child_node)
                else: # Should not happen if _parse_statement is well-defined
                    self._error(f"No progress in block '{key_part_string}' before RBRACE/EOF after consuming a statement token.")
        
        self._consume('RBRACE') 
        
        if self.stack and self.stack[-1] is block_node: self.stack.pop()
        else: self._error(f"Mismatched closing brace for block '{key_part_string}'.")
        return block_node

    def _parse_statement(self):
        # This method now only parses actual statements (KVP, Operator, List, Block).
        # Newlines and comments are handled by the calling loops (parse_file, _parse_block).
        
        token = self._peek()
        if token.type in ['NEWLINE', 'COMMENT', 'EOF']:
            self._error(f"Internal Parser Error: _parse_statement called with {token.type}. Expected a statement token.")

        key_token_peeked = self._peek()
        key_value_str = key_token_peeked.value 
        key_line_num = key_token_peeked.line
        
        node_to_return = None

        if key_token_peeked.type == 'IDENTIFIER' or key_token_peeked.type == 'NUMBER':
            key_token = self._consume(key_token_peeked.type) 
            if key_token.type == 'IDENTIFIER' and \
               self._peek().type == 'IDENTIFIER' and \
               self._peek(1).type in ['EQUALS', 'OPERATOR', 'LBRACE']:
                second_key_part_token = self._consume('IDENTIFIER')
                key_value_str = f"{key_value_str} {second_key_part_token.value}"
            
            next_structural_token_peeked = self._peek()

            if next_structural_token_peeked.type == 'EQUALS':
                self._consume('EQUALS') 
                if self._peek().type == 'LBRACE': 
                    self._consume('LBRACE') 
                    # Heuristic for block vs list
                    is_block_heuristic = False; scan_idx = self.current_token_index; h_brace_balance = 1 
                    while scan_idx < len(self.tokens):
                        h_token = self.tokens[scan_idx]
                        if h_token.type == 'LBRACE': h_brace_balance += 1
                        elif h_token.type == 'RBRACE': h_brace_balance -= 1;                       
                        if h_brace_balance == 0: break 
                        if h_brace_balance < 0: self._error("Malformed braces during block/list detection."); break
                        if h_brace_balance == 1: 
                            if h_token.type in ['NEWLINE', 'COMMENT']: scan_idx += 1; continue 
                            if h_token.type == 'IDENTIFIER' and scan_idx + 1 < len(self.tokens) and self.tokens[scan_idx+1].type in ['EQUALS', 'OPERATOR', 'LBRACE']: is_block_heuristic = True; break
                            elif h_token.type == 'NUMBER' and scan_idx + 1 < len(self.tokens) and self.tokens[scan_idx+1].type == 'EQUALS': is_block_heuristic = True; break
                        if h_token.type == 'EOF': self._error("EOF reached during block/list detection."); break
                        scan_idx += 1
                    
                    block_display_key = f"{key_value_str} =" 
                    if is_block_heuristic: node_to_return = self._parse_block(block_display_key, key_line_num)
                    else: node_to_return = self._parse_list(block_display_key, key_line_num)
                else: node_to_return = self._parse_key_value_pair(key_value_str, key_line_num)
            elif next_structural_token_peeked.type == 'OPERATOR': 
                operator_actual_token = self._consume('OPERATOR') 
                node_to_return = self._parse_operator_condition(key_value_str, operator_actual_token.value, key_line_num)
            elif next_structural_token_peeked.type == 'LBRACE': 
                self._consume('LBRACE')
                node_to_return = self._parse_block(key_value_str, key_line_num)
            else: self._error(f"Unexpected token '{next_structural_token_peeked.type}' after key '{key_value_str}'.")
        else: self._error(f"Statement must start with IDENTIFIER or NUMBER. Got {token.type}")
        
        # Set indent level for top-level statements (root nodes)
        if node_to_return and not self.stack: 
            node_to_return.indent_level = 0
        return node_to_return

    def parse_file(self, filepath):
        self.root_nodes = [] 
        self.stack = []
        self.current_token_index = 0

        if not os.path.exists(filepath): print(f"Error: File not found: {filepath}"); return [] 
        try:
            with open(filepath, 'r', encoding='utf-8-sig') as f: text_content = f.read()
        except UnicodeDecodeError:
            try: 
                with open(filepath, 'r', encoding='utf-8') as f: text_content = f.read()
            except Exception as e_inner: print(f"Error reading file {filepath} with utf-8 fallback: {e_inner}"); return []
        except Exception as e: print(f"Error reading file {filepath}: {e}"); return []
        
        lexer = PdsLexer(text_content)
        try: self.tokens = lexer.tokenize()
        except ValueError as lex_err: print(f"Lexer error in {filepath}: {lex_err}"); return []

        if not self.tokens or self.tokens[0].type == 'EOF': return []
            
        while not self.is_eof():
            token = self._peek()
            if token.type == 'NEWLINE':
                if self._peek(1).type == 'NEWLINE': 
                    self.root_nodes.append(self._parse_blank_line_node())
                else: # Single newline, just consume as separator
                    self._advance()
            elif token.type == 'COMMENT':
                self.root_nodes.append(self._parse_comment())
            elif token.type == 'EOF':
                break # Reached end
            else:
                try:
                    node = self._parse_statement()
                    if node: self.root_nodes.append(node)
                    elif not self.is_eof(): self._error("Parser yielded no node and is not at EOF.") 
                except ValueError as parse_err: print(f"Parser error in {filepath}: {parse_err}"); return [] 
        return self.root_nodes

    @staticmethod
    def _nodes_to_string(nodes_list):
        output = []
        for node in nodes_list:
            if node.indent_level is None : node.indent_level = 0 # Should be set by parser
            output.append(node.to_string(node.indent_level)) 
        return "".join(output)

    def to_string(self):
        return PdsParser._nodes_to_string(self.root_nodes)