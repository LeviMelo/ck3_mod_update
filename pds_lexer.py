import re

class PdsToken:
    """Represents a token found during lexical analysis."""
    def __init__(self, type, value, line, column):
        self.type = type
        self.value = value
        self.line = line
        self.column = column

    def __repr__(self):
        return f"Token(type='{self.type}', value='{self.value}', line={self.line}, col={self.column})"

class PdsLexer:
    """
    Lexer for PDS (Crusader Kings III) script files.
    Breaks input text into a stream of tokens.
    """
    def __init__(self, text):
        self.text = text
        self.pos = 0      # Current position in text
        self.line = 1     # Current line number
        self.column = 1   # Current column number
        self.tokens = []  # List to store generated tokens

        self.single_char_tokens = {
            '{': 'LBRACE',
            '}': 'RBRACE',
            '=': 'EQUALS',
        }
        
        self.patterns = {
            'COMMENT': r'#.*',
            'STRING': r'"[^"]*"',
            'NUMBER': r'-?\d+(?:\.\d+)?',
            'IDENTIFIER': r'[\w\s\.:@\-]+?(?=[\s={}"#]|$)',
        }
        
        self.compiled_patterns = {
            k: re.compile(v) for k, v in self.patterns.items()
        }

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
            
            if char.isspace():
                whitespace_match = re.match(r'\s+', self.text[self.pos:])
                if whitespace_match:
                    whitespace_block = whitespace_match.group(0)
                    current_sub_pos = 0
                    while current_sub_pos < len(whitespace_block):
                        ws_char = whitespace_block[current_sub_pos]
                        if ws_char == '\n':
                            self._add_token_and_advance('NEWLINE', '\n', 1)
                        else:
                            self._advance(1)
                        current_sub_pos += 1
                else:
                    self._advance(1) 
                continue

            if char in self.single_char_tokens:
                self._add_token_and_advance(self.single_char_tokens[char], char, 1)
                continue

            # --- DEBUGGING BLOCK (BROADER CONDITION) ---
            if self.line == 11 and char == '#': # Check if on line 11 AND current char is '#'
                print("--- DEBUGGING BLOCK IN PdsLexer ACTIVATED ---")
                print(f"LEXER_DEBUG: Current char: '{char}' (Unicode ord: {ord(char)}), Line: {self.line}, Column: {self.column}, Pos: {self.pos}")
                context_window = 15
                start_idx = max(0, self.pos - context_window)
                end_idx_context = min(len(self.text), self.pos + context_window + 1) 
                print(f"LEXER_DEBUG: Text context: '{self.text[start_idx:self.pos]}<HERE>{self.text[self.pos:end_idx_context]}'")
                
                slice_for_match = self.text[self.pos:]
                print(f"LEXER_DEBUG: Slice for COMMENT match (first 30 chars): '{slice_for_match[:30]}'")
                
                comment_pattern_obj = self.compiled_patterns['COMMENT']
                direct_match_test = comment_pattern_obj.match(self.text, self.pos) 
                
                if direct_match_test:
                    print(f"LEXER_DEBUG: Direct test of COMMENT pattern SUCCEEDED. Group(0): '{direct_match_test.group(0)}'")
                else:
                    print(f"LEXER_DEBUG: Direct test of COMMENT pattern FAILED! Pattern was: r'{comment_pattern_obj.pattern}'")
                print("--- END LEXER DEBUGGING BLOCK ---")
            # --- END DEBUGGING BLOCK ---

            match_comment = self.compiled_patterns['COMMENT'].match(self.text, self.pos)
            if match_comment:
                comment_text_raw = match_comment.group(0)
                self._add_token_and_advance('COMMENT', comment_text_raw.strip(), len(comment_text_raw))
                continue

            match_string = self.compiled_patterns['STRING'].match(self.text, self.pos)
            if match_string:
                string_text_raw = match_string.group(0)
                self._add_token_and_advance('STRING', string_text_raw, len(string_text_raw))
                continue

            match_number = self.compiled_patterns['NUMBER'].match(self.text, self.pos)
            if match_number:
                number_text_raw = match_number.group(0)
                self._add_token_and_advance('NUMBER', number_text_raw, len(number_text_raw))
                continue

            match_identifier = self.compiled_patterns['IDENTIFIER'].match(self.text, self.pos)
            if match_identifier:
                identifier_text_raw = match_identifier.group(0)
                self._add_token_and_advance('IDENTIFIER', identifier_text_raw.strip(), len(identifier_text_raw))
                continue
            
            raise ValueError(f"Lexer Error: Unexpected character '{char}' at line {self.line}, column {error_reporting_column}")
        
        self.tokens.append(PdsToken('EOF', '', self.line, self.column))
        return self.tokens