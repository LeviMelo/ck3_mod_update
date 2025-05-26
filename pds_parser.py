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
                        self._add_token_and_advance(token_type, token_value_raw[1:].strip(), len(token_value_raw))
                    else:
                        self._add_token_and_advance(token_type, token_value_raw, len(token_value_raw))
                    matched = True
                    break 

            if matched:
                continue 

            raise ValueError(f"Lexer Error: Unexpected character '{char}' at line {self.line}, column {error_reporting_column}")
        
        self.tokens.append(PdsToken('EOF', '', self.line, self.column)) 
        return self.tokens

# --- PdsNode Classes ---

class PdsNode:
    def __init__(self, line_number=-1): 
        self.line_number = line_number
        self.indent_level = 0 

    def to_string(self, current_indent=0):
        raise NotImplementedError(f"to_string() not implemented for {self.__class__.__name__}")

    def __repr__(self):
        key_repr = getattr(self, 'key', 'N/A')
        if not isinstance(key_repr, str): key_repr = str(key_repr)
        comment_repr = getattr(self, 'comment_text_on_line', None)
        comment_str = f", CommentText='{comment_repr[:20]}...'" if comment_repr else ""
        
        display_class_name = self.__class__.__name__
        specific_repr = ""
        if isinstance(self, PdsComment): specific_repr = f"Comment: '{getattr(self, 'comment_text', '')[:30]}...'"
        elif isinstance(self, PdsBlankLine): specific_repr = "Blank Line"
        elif isinstance(self, PdsList): specific_repr = f"{key_repr}={{...}}"
        elif isinstance(self, PdsOperatorCondition): specific_repr = f"{key_repr} {self.operator} {self.value}"
        elif isinstance(self, PdsBlock): specific_repr = f"{key_repr}={{...}}"
        elif isinstance(self, PdsKeyValuePair) and isinstance(self.value, PdsBlock): specific_repr = f"{key_repr}={{...}}"
        else: specific_repr = f"K='{key_repr}'"
        
        return f"<{display_class_name} L{self.line_number} I{self.indent_level} {specific_repr}{comment_str}>"

    def copy(self): # Basic copy, subclasses should override for deep copy if they have complex mutable members
        new_node = self.__class__(line_number=self.line_number)
        new_node.indent_level = self.indent_level
        # Copy other common simple attributes if any
        if hasattr(self, 'comment_text_on_line'):
            new_node.comment_text_on_line = self.comment_text_on_line
        return new_node


class PdsComment(PdsNode):
    def __init__(self, comment_text, line_number=-1):
        super().__init__(line_number=line_number)
        self.comment_text = comment_text

    def to_string(self, current_indent=0):
        indent_str = " " * current_indent
        return f"{indent_str}# {self.comment_text}\n"

    def copy(self):
        new_node = super().copy()
        new_node.comment_text = self.comment_text
        return new_node

class PdsBlankLine(PdsNode):
    def __init__(self, line_number=-1):
        super().__init__(line_number=line_number)

    def to_string(self, current_indent=0):
        return "\n"

    def copy(self):
        return super().copy()


class PdsKeyValuePair(PdsNode):
    _lexer_identifier_pattern = r'[\w\.:@\-]+' 

    def __init__(self, key, value, line_number=-1, comment_text=None):
        super().__init__(line_number=line_number)
        self.key = key
        self.value = value 
        self.comment_text_on_line = comment_text

    def to_string(self, current_indent=0):
        indent_str = " " * current_indent
        value_output_str = ""

        if isinstance(self.value, PdsBlock):
             value_output_str = "\n" + self.value.to_string(current_indent)
        elif isinstance(self.value, str):
            must_quote = (
                ' ' in self.value or '\t' in self.value or
                '#' in self.value or '=' in self.value or
                '{' in self.value or '}' in self.value or
                '"' in self.value or not self.value 
            )
            is_simple_identifier = re.fullmatch(PdsKeyValuePair._lexer_identifier_pattern, self.value)
            
            if must_quote or (self.value and not is_simple_identifier): # Ensure non-empty non-identifiers are quoted
                 value_output_str = f'"{self.value}"'
            else:
                value_output_str = self.value
        else: 
            value_output_str = str(self.value)
        
        line_content = f"{indent_str}{self.key} = {value_output_str}"
        if self.comment_text_on_line:
            line_content += f" # {self.comment_text_on_line}"
        
        if isinstance(self.value, PdsBlock):
            return line_content 
        else:
            return line_content + "\n"

    def copy(self):
        new_node = super().copy()
        new_node.key = self.key
        new_node.value = self.value.copy() if isinstance(self.value, PdsNode) else self.value
        # comment_text_on_line is handled by super().copy() if it's a common attribute
        return new_node

class PdsList(PdsNode): 
    def __init__(self, key, values, line_number=-1, comment_text=None): 
        super().__init__(line_number=line_number)
        self.key = key
        self.values = values 
        self.comment_text_on_line = comment_text

    def to_string(self, current_indent=0):
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
        new_node = super().copy()
        new_node.key = self.key
        new_node.values = list(self.values)
        return new_node

class PdsOperatorCondition(PdsNode):
    def __init__(self, key, operator, value, line_number=-1, comment_text=None):
        super().__init__(line_number=line_number)
        self.key = key
        self.operator = operator 
        self.value = value        
        self.comment_text_on_line = comment_text

    def to_string(self, current_indent=0):
        indent_str = " " * current_indent
        value_output_str = ""

        if isinstance(self.value, PdsBlock):
            value_output_str = "\n" + self.value.to_string(current_indent)
        elif isinstance(self.value, str):
            must_quote = (
                ' ' in self.value or '\t' in self.value or
                '#' in self.value or '=' in self.value or
                '{' in self.value or '}' in self.value or
                '"' in self.value or not self.value
            )
            is_simple_identifier = re.fullmatch(PdsKeyValuePair._lexer_identifier_pattern, self.value)
            if must_quote or (self.value and not is_simple_identifier):
                 value_output_str = f'"{self.value}"'
            else:
                value_output_str = self.value
        else:
            value_output_str = str(self.value)

        line_content = f"{indent_str}{self.key} {self.operator} {value_output_str}"
        if self.comment_text_on_line:
            line_content += f" # {self.comment_text_on_line}"
        
        if isinstance(self.value, PdsBlock):
            return line_content
        else:
            return line_content + "\n"

    def copy(self):
        new_node = super().copy()
        new_node.key = self.key
        new_node.operator = self.operator
        new_node.value = self.value.copy() if isinstance(self.value, PdsNode) else self.value
        return new_node

class PdsBlock(PdsNode):
    def __init__(self, key, line_number=-1, comment_text=None):
        super().__init__(line_number=line_number)
        self.key = key 
        self.children = []
        self.comment_text_on_line = comment_text

    def add_child(self, node):
        node.indent_level = self.indent_level + 4 
        self.children.append(node)

    def to_string(self, current_indent=0):
        indent_str = " " * current_indent
        child_indent = current_indent + 4 
        output_lines = []

        open_brace_line = indent_str
        if self.key: 
            open_brace_line += f"{self.key} {{"
        else: 
            open_brace_line += "{"

        if self.comment_text_on_line: 
            open_brace_line += f" # {self.comment_text_on_line}" 
        output_lines.append(open_brace_line + "\n")

        for child in self.children:
            output_lines.append(child.to_string(child_indent))
        
        output_lines.append(f"{indent_str}}}\n") 
        return "".join(output_lines)
    
    def find_node(self, key_path):
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

    def copy(self):
        new_node = super().copy()
        new_node.key = self.key
        new_node.children = [child.copy() for child in self.children] 
        return new_node

    def replace_child(self, old_child_identifier, new_child_node):
        for i, child in enumerate(self.children):
            child_key_str = str(child.key) if hasattr(child, 'key') else None
            if child_key_str == str(old_child_identifier): 
                self.children[i] = new_child_node; return True
        return False

    def remove_child(self, child_identifier):
        original_len = len(self.children)
        self.children = [c for c in self.children if not (hasattr(c, 'key') and str(c.key) == str(child_identifier))]
        return len(self.children) < original_len

    def add_child_at_appropriate_location(self, new_child_node, target_sibling_identifier=None, after=True):
        if target_sibling_identifier:
            for i, child in enumerate(self.children):
                child_key_str = str(child.key) if hasattr(child, 'key') else None
                if child_key_str == str(target_sibling_identifier): 
                    self.children.insert(i + 1 if after else i, new_child_node); return True
        last_code_node_idx = -1
        for i in reversed(range(len(self.children))):
            if not isinstance(self.children[i], (PdsComment, PdsBlankLine)):
                last_code_node_idx = i; break
        if last_code_node_idx != -1: self.children.insert(last_code_node_idx + 1, new_child_node)
        else: self.children.append(new_child_node)
        return True

# --- PdsParser ---
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
        return PdsToken('EOF', '', self.tokens[-1].line if self.tokens else -1, self.tokens[-1].column if self.tokens else -1)

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

    def _parse_blank_line(self):
        # Assumes the calling context decided this is a "true" blank line.
        # Consumes all consecutive NEWLINE tokens from the current position.
        first_token = self._peek() # Should be NEWLINE
        line_number = first_token.line
        while self._peek().type == 'NEWLINE':
            self._advance()
        return PdsBlankLine(line_number)

    def _parse_value(self):
        token = self._peek()
        if token.type == 'LBRACE':
            lbrace_token = self._consume('LBRACE') 
            return self._parse_block("", lbrace_token.line) 
        elif token.type == 'IDENTIFIER':
            value_token = self._consume('IDENTIFIER')
            return value_token.value 
        elif token.type == 'NUMBER':
            value_token = self._consume('NUMBER')
            try:
                if '.' in value_token.value: return float(value_token.value)
                else: return int(value_token.value)
            except ValueError: return value_token.value 
        elif token.type == 'STRING':
            value_token = self._consume('STRING')
            return value_token.value[1:-1] 
        self._error(f"Expected a value (IDENTIFIER, NUMBER, STRING, or LBRACE for a block)")

    def _parse_key_value_pair(self, key_string, key_line_number):
        value = self._parse_value()
        return PdsKeyValuePair(key_string, value, key_line_number)

    def _parse_operator_condition(self, key_string, operator_str, key_line_number):
        value = self._parse_value() 
        return PdsOperatorCondition(key_string, operator_str, value, key_line_number)

    def _parse_list(self, key_part_string, key_line_number): 
        values = []
        while self._peek().type != 'RBRACE' and not self.is_eof():
            token = self._peek()
            if token.type in ['IDENTIFIER', 'NUMBER', 'STRING']:
                values.append(self._parse_value()) 
            elif token.type == 'NEWLINE' or token.type == 'COMMENT': 
                self._advance() 
            else:
                self._error(f"Unexpected token {token.type} inside PdsList values.")
        self._consume('RBRACE') 
        return PdsList(key_part_string, values, key_line_number) 

    def _parse_block(self, key_part_string, key_line_number): 
        block_node = PdsBlock(key_part_string, key_line_number) 
        self.stack.append(block_node)
        while self._peek().type != 'RBRACE' and not self.is_eof():
            child_node = self._parse_statement() 
            if child_node:
                block_node.add_child(child_node)
            # If _parse_statement returns None (e.g. for skipped separator newlines), we just loop.
            elif self.is_eof() or self._peek().type == 'RBRACE': # Break if loop should end
                break
            else: # Should not happen if _parse_statement advances or errors
                 self._error(f"Could not parse statement inside block '{key_part_string}' and not at end of block.")
        self._consume('RBRACE') 
        if self.stack and self.stack[-1] is block_node: self.stack.pop()
        else: self._error(f"Mismatched closing brace for block '{key_part_string}'.")
        return block_node

    def _parse_statement(self):
        # Skip leading single newlines that are just separators and don't form a PdsBlankLine node.
        # A PdsBlankLine node is created by _parse_blank_line() only if it sees multiple newlines
        # or a single newline that is truly acting as a separator (e.g., before RBRACE/EOF).
        while self._peek().type == 'NEWLINE':
            # If this NEWLINE is followed by something other than another NEWLINE,
            # and that something is not RBRACE or EOF (where a preceding blank line might be intended),
            # then it's likely a formatting newline, not a blank line node itself.
            next_token_after_newline = self._peek(1)
            if next_token_after_newline.type not in ['NEWLINE', 'RBRACE', 'EOF']:
                self._advance() # Consume the separator newline and loop to check again
            else:
                # This newline is either part of a multi-newline sequence,
                # or it's a single newline before RBRACE/EOF.
                # In these cases, we let it be handled by the NEWLINE case below,
                # which will call _parse_blank_line().
                break 
        
        # After skipping separator newlines, proceed with parsing the actual statement
        token = self._peek()
        # statement_start_line = token.line # Line number of the *actual* start of the statement

        if token.type == 'COMMENT':
            return self._parse_comment()
        elif token.type == 'NEWLINE': 
            # This is reached if the loop above breaks, meaning we have:
            # 1. A sequence of NEWLINEs (e.g., NEWLINE, NEWLINE, ...)
            # 2. A single NEWLINE followed by RBRACE
            # 3. A single NEWLINE followed by EOF
            # These are considered candidates for PdsBlankLine nodes.
            return self._parse_blank_line()
        
        # --- Key parsing and statement determination for IDENTIFIER or NUMBER keys ---
        key_token = self._peek()
        key_value_str = key_token.value 
        key_line_num = key_token.line

        if key_token.type == 'IDENTIFIER' or key_token.type == 'NUMBER':
            self._consume(key_token.type) 

            if key_token.type == 'IDENTIFIER' and \
               self._peek().type == 'IDENTIFIER' and \
               self._peek(1).type in ['EQUALS', 'OPERATOR', 'LBRACE']:
                second_key_part_token = self._consume('IDENTIFIER')
                key_value_str = f"{key_value_str} {second_key_part_token.value}"
            
            next_structural_token = self._peek()

            if next_structural_token.type == 'EQUALS':
                self._consume('EQUALS') 
                if self._peek().type == 'LBRACE': 
                    self._consume('LBRACE') 
                    is_block_heuristic = False 
                    scan_idx = self.current_token_index 
                    h_brace_balance = 1 
                    while scan_idx < len(self.tokens):
                        h_token = self.tokens[scan_idx]
                        if h_token.type == 'LBRACE': h_brace_balance += 1
                        elif h_token.type == 'RBRACE':
                            h_brace_balance -= 1
                            if h_brace_balance == 0: break 
                        if h_brace_balance < 0: self._error("Malformed braces during block/list detection."); break
                        
                        if h_brace_balance == 1: 
                            if h_token.type in ['NEWLINE', 'COMMENT']: scan_idx += 1; continue 
                            if h_token.type == 'IDENTIFIER':
                                if scan_idx + 1 < len(self.tokens) and \
                                   self.tokens[scan_idx+1].type in ['EQUALS', 'OPERATOR', 'LBRACE']:
                                    is_block_heuristic = True; break
                            elif h_token.type == 'NUMBER':
                                 if scan_idx + 1 < len(self.tokens) and \
                                    self.tokens[scan_idx+1].type == 'EQUALS':
                                    is_block_heuristic = True; break
                        if h_token.type == 'EOF': self._error("EOF reached during block/list detection."); break
                        scan_idx += 1
                    
                    block_display_key = f"{key_value_str} =" 
                    if is_block_heuristic:
                        return self._parse_block(block_display_key, key_line_num)
                    else:
                        return self._parse_list(block_display_key, key_line_num)
                else: 
                    return self._parse_key_value_pair(key_value_str, key_line_num)

            elif next_structural_token.type == 'OPERATOR': 
                operator_actual_token = self._consume('OPERATOR') 
                return self._parse_operator_condition(key_value_str, operator_actual_token.value, key_line_num)

            elif next_structural_token.type == 'LBRACE': 
                self._consume('LBRACE')
                return self._parse_block(key_value_str, key_line_num) 
            
            else:
                self._error(f"Unexpected token '{next_structural_token.type}' after key '{key_value_str}'. Expected '=', operator, or '{{'.")
        
        elif token.type == 'EOF': # If, after skipping newlines, we hit EOF
            return None # No more statements to parse
            
        else: 
            self._error(f"Statement must start with IDENTIFIER, NUMBER, COMMENT, or be a blank line.")


    def parse_file(self, filepath):
        self.root_nodes = [] 
        self.stack = []
        self.current_token_index = 0

        if not os.path.exists(filepath):
            print(f"Error: File not found: {filepath}"); return [] 
        try:
            with open(filepath, 'r', encoding='utf-8-sig') as f: text_content = f.read()
        except UnicodeDecodeError:
            try: 
                with open(filepath, 'r', encoding='utf-8') as f: text_content = f.read()
            except Exception as e_inner: 
                print(f"Error reading file {filepath} with utf-8 fallback: {e_inner}"); return []
        except Exception as e: 
            print(f"Error reading file {filepath}: {e}"); return []
        
        lexer = PdsLexer(text_content)
        try:
            self.tokens = lexer.tokenize()
        except ValueError as lex_err:
            print(f"Lexer error in {filepath}: {lex_err}"); return []

        if not self.tokens or self.tokens[0].type == 'EOF': return []
            
        while not self.is_eof():
            try:
                # _parse_statement will now handle skipping its own leading separator newlines
                # or returning a PdsBlankLine node if appropriate.
                node = self._parse_statement()
                if node:
                    self.root_nodes.append(node)
                elif not self.is_eof(): # If node is None but not EOF, something unexpected
                    # This case should be rare if _parse_statement correctly consumes or errors.
                    # It might happen if _parse_statement returns None for EOF effectively.
                    # Check if we are at EOF after _parse_statement returned None.
                    # The while loop condition `not self.is_eof()` handles exiting.
                    # If _parse_statement returns None and it's not EOF, that's an issue.
                    # The EOF case in _parse_statement returning None is now explicit.
                     self._error("Parser yielded no node and is not at EOF.") # Safety error
            except ValueError as parse_err:
                print(f"Parser error in {filepath}: {parse_err}")
                return [] 
        return self.root_nodes

    @staticmethod
    def _nodes_to_string(nodes_list):
        output = []
        for node in nodes_list:
            output.append(node.to_string(node.indent_level)) 
        return "".join(output)

    def to_string(self):
        return PdsParser._nodes_to_string(self.root_nodes)