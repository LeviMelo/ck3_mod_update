import re
import os
import difflib

# --- PdsLexer and PdsToken (Integrated from your script) ---

class PdsToken:
    """Represents a token found during lexical analysis."""
    def __init__(self, type, value, line, column):
        self.type = type
        self.value = value
        self.line = line
        self.column = column

    def __repr__(self):
        # Ensure value is truncated if too long for cleaner output
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
        self.pos = 0        # Current position in text
        self.line = 1       # Current line number
        self.column = 1     # Current column number
        self.tokens = []    # List to store generated tokens

        self.single_char_tokens = {
            '{': 'LBRACE',
            '}': 'RBRACE',
            '=': 'EQUALS',
            # Add other single-character operators if PDS uses them outside identifiers
            # For now, combined operators are handled by IDENTIFIER or specific regex
        }
        
        # Order of patterns matters for greedy matching (e.g., COMMENT before IDENTIFIER)
        # IDENTIFIER regex adjusted: does NOT include whitespace unless part of a quoted string (handled by STRING type)
        # Operators like >=, <=, !=, ==, ?= are now explicitly matched if they are standalone operators.
        self.patterns = [
            ('COMMENT', r'#.*'),
            ('OPERATOR', r'(?:>=|<=|==|!=|\?=|>|<|!)'), # Multi-char operators (e.g., ?=)
            ('STRING', r'"[^"]*"'), # Quoted strings
            ('NUMBER', r'-?\d+(?:\.\d+)?'),
            # IDENTIFIER: Matches alphanumeric, _, ., :, @, - characters.
            # It must not match operators, numbers, or start with a digit if it's an identifier.
            # Using a lookahead to ensure it doesn't consume beyond a separator.
            ('IDENTIFIER', r'[\w\.:@\-]+(?:[\w\.:@\-]*[\w\.:@\-])?'), # Adjusted IDENTIFIER pattern
        ]
        
        # Compile patterns for efficiency
        self.compiled_patterns = [(k, re.compile(v)) for k, v in self.patterns]

    def _advance(self, count):
        """Advances the internal position and updates line/column."""
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
        """Adds a token to the list and advances position by advance_length."""
        self.tokens.append(PdsToken(token_type, token_value_for_storage, self.line, self.column))
        self._advance(advance_length)

    def tokenize(self):
        while self.pos < len(self.text):
            char = self.text[self.pos]
            error_reporting_column = self.column 
            
            # 1. Handle Whitespace (including NEWLINE)
            if char.isspace():
                whitespace_match = re.match(r'\s+', self.text[self.pos:])
                if whitespace_match:
                    whitespace_block = whitespace_match.group(0)
                    for ws_char in whitespace_block:
                        if ws_char == '\n':
                            self._add_token_and_advance('NEWLINE', '\n', 1)
                        else:
                            self._advance(1) # Just advance for other whitespace
                continue # Continue to next char after handling whitespace

            # 2. Handle Single-Character Tokens
            if char in self.single_char_tokens:
                self._add_token_and_advance(self.single_char_tokens[char], char, 1)
                continue

            # 3. Handle Multi-Character Patterns (in defined order)
            matched = False
            for token_type, pattern_regex in self.compiled_patterns:
                match = pattern_regex.match(self.text, self.pos)
                if match:
                    token_value_raw = match.group(0)
                    # Special handling for IDENTIFIER: strip leading/trailing whitespace if any
                    # (though IDENTIFIER pattern should ideally avoid capturing it)
                    # For COMMENT, store without # and leading/trailing whitespace
                    if token_type == 'COMMENT':
                        self._add_token_and_advance(token_type, token_value_raw[1:].strip(), len(token_value_raw))
                    elif token_type == 'IDENTIFIER':
                         # If IDENTIFIER ends with whitespace due to lookahead (which it shouldn't with my new regex)
                         # or if it was designed to capture spaces within the identifier (like "some key")
                         # strip it here. My new IDENTIFIER regex should NOT capture whitespace.
                        self._add_token_and_advance(token_type, token_value_raw.strip(), len(token_value_raw))
                    else:
                        self._add_token_and_advance(token_type, token_value_raw, len(token_value_raw))
                    matched = True
                    break # Move to next position in text after a successful match

            if matched:
                continue # Continue to next char after handling a pattern

            # If no rule matched, it's an error
            raise ValueError(f"Lexer Error: Unexpected character '{char}' at line {self.line}, column {error_reporting_column}")
        
        self.tokens.append(PdsToken('EOF', '', self.line, self.column)) # End Of File token
        return self.tokens

# --- PdsNode Classes (Adapted for token-based parsing) ---

class PdsNode:
    """Base class for all elements in the PDS script tree."""
    def __init__(self, line_number=-1): # raw_line and indent_level will be derived during reconstruction
        self.line_number = line_number
        self.indent_level = 0 # Will be set by the parser when adding to block

    def to_string(self, current_indent=0):
        # This method MUST be overridden by all concrete node types.
        # It should reconstruct the string representation of the node.
        # For base class, it's a structural error if called.
        raise NotImplementedError(f"to_string() not implemented for {self.__class__.__name__}")

    def __repr__(self):
        key_repr = getattr(self, 'key', 'N/A')
        comment_repr = getattr(self, 'comment_text_on_line', None)
        comment_str = f", CommentText='{comment_repr[:20]}...'" if comment_repr else ""
        
        if isinstance(self, PdsComment):
            key_repr = f"Comment: '{getattr(self, 'comment_text', '')[:30]}...'"
        elif isinstance(self, PdsBlankLine):
            key_repr = "Blank Line"
        elif isinstance(self, PdsList):
            key_repr = f"{self.key}={{...}}"
        elif isinstance(self, PdsOperatorCondition):
            key_repr = f"{self.key} {self.operator} {self.value}"
        elif isinstance(self, PdsBlock): 
            key_repr = f"{self.key}={{...}}"
        
        return f"<{self.__class__.__name__} L{self.line_number} I{self.indent_level} K='{key_repr}'{comment_str}>"

    def copy(self):
        # Generic copy for basic PdsNode attributes. Subclasses will need to override.
        return self.__class__(
            line_number=self.line_number
        )

class PdsComment(PdsNode):
    def __init__(self, comment_text, line_number=-1):
        super().__init__(line_number=line_number)
        self.comment_text = comment_text

    def to_string(self, current_indent=0):
        indent_str = " " * current_indent
        # Standardize comment formatting: ensure one space after #
        # self.comment_text should already be stripped by lexer
        return f"{indent_str}# {self.comment_text}\n"

    def copy(self):
        new_node = self.__class__(
            comment_text=self.comment_text,
            line_number=self.line_number
        )
        new_node.indent_level = self.indent_level
        return new_node

class PdsBlankLine(PdsNode):
    def __init__(self, line_number=-1):
        super().__init__(line_number=line_number)

    def to_string(self, current_indent=0):
        return "\n" # Blank line is just a newline

    def copy(self):
        new_node = self.__class__(
            line_number=self.line_number
        )
        new_node.indent_level = self.indent_level
        return new_node


class PdsKeyValuePair(PdsNode):
    def __init__(self, key, value, line_number=-1, comment_text=None):
        super().__init__(line_number=line_number)
        self.key = key
        self.value = value 
        self.comment_text_on_line = comment_text

    def to_string(self, current_indent=0):
        indent_str = " " * current_indent
        value_str = str(self.value) 
        if self.comment_text_on_line:
            return f"{indent_str}{self.key} = {value_str} # {self.comment_text_on_line}\n"
        return f"{indent_str}{self.key} = {value_str}\n"

    def copy(self):
        new_node = self.__class__(
            key=self.key,
            value=self.value,
            line_number=self.line_number,
            comment_text=self.comment_text_on_line
        )
        new_node.indent_level = self.indent_level
        return new_node


class PdsList(PdsNode): 
    def __init__(self, key, values, line_number=-1, comment_text=None):
        super().__init__(line_number=line_number)
        self.key = key
        self.values = values 
        self.comment_text_on_line = comment_text

    def to_string(self, current_indent=0):
        indent_str = " " * current_indent
        value_str = " ".join(str(v) for v in self.values) # Ensure values are strings for join
        inner_content = f" {value_str} " if self.values else " " 
        base_string = f"{indent_str}{self.key} = {{{inner_content}}}"
        if self.comment_text_on_line:
            return f"{base_string} # {self.comment_text_on_line}\n"
        return f"{base_string}\n"

    def copy(self):
        new_node = self.__class__(
            key=self.key,
            values=list(self.values), 
            line_number=self.line_number,
            comment_text=self.comment_text_on_line
        )
        new_node.indent_level = self.indent_level
        return new_node

class PdsOperatorCondition(PdsNode):
    """Represents a statement like 'count >= 1' or 'owner ?= this'.
        The operator is not always '=' and it does not define a block.
    """
    def __init__(self, key, operator, value, line_number=-1, comment_text=None):
        super().__init__(line_number=line_number)
        self.key = key
        self.operator = operator 
        self.value = value        
        self.comment_text_on_line = comment_text

    def to_string(self, current_indent=0):
        indent_str = " " * current_indent
        line = f"{indent_str}{self.key} {self.operator} {self.value}"
        if self.comment_text_on_line:
            return f"{line} # {self.comment_text_on_line}\n"
        return f"{line}\n"

    def copy(self):
        new_node = self.__class__(
            key=self.key,
            operator=self.operator,
            value=self.value,
            line_number=self.line_number,
            comment_text=self.comment_text_on_line
        )
        new_node.indent_level = self.indent_level
        return new_node

class PdsBlock(PdsNode):
    def __init__(self, key, line_number=-1, comment_text=None):
        super().__init__(line_number=line_number)
        self.key = key 
        self.children = []
        self.comment_text_on_line = comment_text

    def add_child(self, node):
        node.indent_level = self.indent_level + 4 # Set child indent relative to parent
        self.children.append(node)

    def to_string(self, current_indent=0):
        indent_str = " " * current_indent
        child_indent = current_indent + 4 
        output_lines = []

        open_brace_line = f"{indent_str}{self.key} {{" 
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
                if hasattr(node, 'key') and node.key == segment: 
                    if i == len(key_path) - 1: return node 
                    elif isinstance(node, PdsBlock): 
                        found_node_for_segment = node
                        break 
                    else: 
                        return None 
            
            if found_node_for_segment: 
                current_nodes_to_search = found_node_for_segment.children
            else: 
                return None 
        return None 

    def copy(self):
        new_node = self.__class__(
            key=self.key,
            line_number=self.line_number,
            comment_text=self.comment_text_on_line
        )
        new_node.indent_level = self.indent_level
        new_node.children = [child.copy() for child in self.children] # Deep copy children recursively
        return new_node

    def replace_child(self, old_child_identifier, new_child_node):
        from pds_differ import PdsDiffer 
        differ_util = PdsDiffer() 

        for i, child in enumerate(self.children):
            if differ_util._get_node_identifier(child) == old_child_identifier:
                self.children[i] = new_child_node
                return True
        return False

    def remove_child(self, child_identifier):
        from pds_differ import PdsDiffer
        differ_util = PdsDiffer()
        
        original_len = len(self.children)
        self.children = [
            child for child in self.children 
            if differ_util._get_node_identifier(child) != child_identifier
        ]
        return len(self.children) < original_len

    def add_child_at_appropriate_location(self, new_child_node, target_sibling_identifier=None, after=True):
        from pds_differ import PdsDiffer
        differ_util = PdsDiffer()

        if target_sibling_identifier:
            for i, child in enumerate(self.children):
                if differ_util._get_node_identifier(child) == target_sibling_identifier:
                    insert_idx = i + 1 if after else i
                    self.children.insert(insert_idx, new_child_node)
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


class PdsParser:
    def __init__(self):
        self.tokens = []
        self.current_token_index = 0
        self.current_token = None
        self.root_nodes = []
        self.stack = [] # Used for block nesting

    def _error(self, message):
        token_info = ""
        if self.current_token:
            token_info = f"at '{self.current_token.value}' (type: {self.current_token.type}) on line {self.current_token.line}, column {self.current_token.column}"
        raise ValueError(f"Parser Error: {message} {token_info}")

    def _peek(self):
        """Returns the current token without consuming it."""
        if self.current_token_index < len(self.tokens):
            return self.tokens[self.current_token_index]
        return PdsToken('EOF', '', -1, -1) # Return EOF token if at end

    def _advance(self):
        """Consumes the current token and moves to the next."""
        self.current_token_index += 1
        self.current_token = self._peek()

    def _consume(self, *expected_types):
        """Consumes the current token if its type matches one of the expected types.
        Advances to the next token. Raises an error if type does not match.
        """
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
        token = self._consume('NEWLINE') # Only consuming one NEWLINE for a blank line
        # Logic to handle multiple NEWLINEs, or sequence of NEWLINEs
        # For simplicity, if we get a NEWLINE token, we assume it's a blank line node.
        # The lexer handles multiple raw newlines into multiple NEWLINE tokens.
        return PdsBlankLine(token.line)

    def _parse_identifier(self):
        token = self._consume('IDENTIFIER')
        return token.value

    def _parse_value(self):
        # A value can be an IDENTIFIER, NUMBER, or STRING
        token = self._peek()
        if token.type in ['IDENTIFIER', 'NUMBER', 'STRING']:
            self._advance()
            return token.value
        self._error(f"Expected a value (IDENTIFIER, NUMBER, or STRING), got {token.type}")

    def _parse_key_value_pair(self):
        key_token = self._consume('IDENTIFIER')
        self._consume('EQUALS')
        value = self._parse_value()
        return PdsKeyValuePair(key_token.value, value, key_token.line)

    def _parse_operator_condition(self):
        key_token = self._consume('IDENTIFIER')
        operator_token = self._consume('OPERATOR')
        value = self._parse_value()
        return PdsOperatorCondition(key_token.value, operator_token.value, value, key_token.line)

    def _parse_list(self, key_value, line_number):
        # Already consumed KEY and EQUALS, now expect LBRACE
        values = []
        
        while self._peek().type != 'RBRACE' and not self.is_eof():
            value_token = self._peek()
            if value_token.type in ['IDENTIFIER', 'NUMBER', 'STRING']:
                values.append(self._parse_value())
            elif value_token.type == 'COMMENT': # Allow comments inside lists
                # For simplicity, comments inside lists are currently ignored for list values,
                # but could be parsed as PdsComment nodes if needed.
                self._advance() # Consume comment token
            elif value_token.type == 'NEWLINE': # Allow newlines inside lists
                self._advance() # Consume newline token
            else:
                self._error(f"Expected value, comment, or NEWLINE in list, got {value_token.type}")
        
        self._consume('RBRACE')
        return PdsList(key_value, values, line_number)

    def _parse_block(self, key_value, line_number):
        # Already consumed KEY (or KEY OPERATOR) and EQUALS (if present), now expect LBRACE
        block_node = PdsBlock(key_value, line_number)
        
        # Consume tokens until RBRACE or EOF
        while self._peek().type != 'RBRACE' and not self.is_eof():
            child_node = self._parse_statement() # Recursively parse children
            if child_node:
                block_node.add_child(child_node)
            else:
                # Handle unparseable tokens or break out if unhandled
                if self._peek().type != 'RBRACE': # Prevent infinite loop on unhandled tokens
                    self._error(f"Could not parse statement inside block, current token: {self._peek().type}")
        
        self._consume('RBRACE') # Consume the closing brace
        return block_node

    def _parse_statement(self):
        """Parses a single statement, which can be a KV pair, Operator Condition, List, Block, Comment, or Blank Line."""
        token = self._peek()
        line_number = token.line # Get line number from the starting token

        if token.type == 'COMMENT':
            return self._parse_comment()
        elif token.type == 'NEWLINE':
            return self._parse_blank_line()
        elif token.type == 'IDENTIFIER':
            # Look ahead to determine type: KV, Operator Condition, List, or Block
            next_token = self.tokens[self.current_token_index + 1] if self.current_token_index + 1 < len(self.tokens) else None
            
            if next_token and next_token.type == 'EQUALS':
                if self.tokens[self.current_token_index + 2].type == 'LBRACE': # key = {
                    # Consume key and equals, then pass to parse_block/parse_list
                    key = self._consume('IDENTIFIER').value
                    self._consume('EQUALS')
                    self._consume('LBRACE') # Consume LBRACE
                    
                    # Need to check if it's a list or a block here
                    # PDS convention: key = { item1 item2 } is a list
                    #                  key = { nested_key = value } is a block
                    
                    # Heuristic: If the next non-whitespace/non-comment token is an IDENTIFIER followed by EQUALS/OPERATOR/LBRACE,
                    # it's a block. Otherwise, it's a list.
                    
                    # Peek after LBRACE to see if it's a structured element or just values.
                    peek_after_lbrace_index = self.current_token_index
                    while peek_after_lbrace_index < len(self.tokens) and \
                          self.tokens[peek_after_lbrace_index].type in ['NEWLINE', 'COMMENT']:
                        peek_after_lbrace_index += 1
                    
                    if peek_after_lbrace_index < len(self.tokens) and \
                       self.tokens[peek_after_lbrace_index].type == 'IDENTIFIER':
                        
                        peek_after_key_index = peek_after_lbrace_index + 1
                        if peek_after_key_index < len(self.tokens) and \
                           self.tokens[peek_after_key_index].type in ['EQUALS', 'OPERATOR', 'LBRACE']:
                            # It's a block (e.g., 'key = { nested_key = value }' or 'key = { nested_key { } }')
                            # Pass the key (including " = "), and line number to _parse_block.
                            return self._parse_block(key + " =", line_number)
                    
                    # If not a block, it's a list.
                    return self._parse_list(key + " =", line_number)
                else: # key = value
                    return self._parse_key_value_pair()
            elif next_token and next_token.type == 'OPERATOR': # key OPERATOR value
                return self._parse_operator_condition()
            elif next_token and next_token.type == 'LBRACE': # key { (without '=') - e.g., "effect = { random_event = { ... }}" or "owner = { ... } (if no '=')"
                # PDS often allows "key {" for blocks like "scripted_effect = { ... }"
                # In this case, the KEY does not include "=" or operator
                key = self._consume('IDENTIFIER').value
                self._consume('LBRACE')
                # This is always a block if it's key {
                return self._parse_block(key, line_number)
            else:
                self._error(f"Unexpected token sequence starting with {token.type}. Expected a statement.")
        
        # If execution reaches here, it means no known statement type matched
        self._error(f"Unexpected token: {token.type} - {token.value}")
        return None # Should not be reached

    def parse_file(self, filepath):
        if not os.path.exists(filepath):
            print(f"Error: File not found: {filepath}"); 
            return []
        try:
            with open(filepath, 'r', encoding='utf-8-sig') as f: text_content = f.read()
        except UnicodeDecodeError:
            try: 
                with open(filepath, 'r', encoding='utf-8') as f: text_content = f.read()
            except Exception as e_inner: 
                print(f"Error reading file {filepath} with utf-8 fallback: {e_inner}"); 
                return []
        except Exception as e: 
            print(f"Error reading file {filepath}: {e}"); 
            return []
        
        lexer = PdsLexer(text_content)
        self.tokens = lexer.tokenize()
        
        # Start parsing the document
        self.root_nodes = []
        self.current_token_index = 0
        self.current_token = self._peek() # Initialize current_token
        
        # Parse until EOF
        while not self.is_eof():
            node = self._parse_statement()
            if node:
                self.root_nodes.append(node)
            else:
                # Advance to prevent infinite loop on unhandled tokens
                self._error(f"Failed to parse statement at top level, current token: {self._peek()}")
                break # Break to avoid endless loop on error
        
        return self.root_nodes

    @staticmethod
    def _nodes_to_string(nodes_list):
        output = []
        for node in nodes_list:
            output.append(node.to_string(node.indent_level)) 
        return "".join(output)

    def to_string(self):
        return PdsParser._nodes_to_string(self.root_nodes)