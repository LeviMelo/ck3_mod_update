import re

class PdsPostProcessor:
    """
    Applies regex-based formatting adjustments to PDS script content.
    Intended to be run AFTER parsing and reconstruction, to normalize newlines.
    The goal is to fix any minor stylistic inconsistencies not handled by the AST.
    """
    def __init__(self):
        # List of regex patterns and their replacements.
        # Order matters: more general patterns or patterns that create new valid contexts should come first.
        self.patterns = [
            # 1. Normalize multiple blank lines to a single blank line (any sequence of 3+ newlines becomes 2)
            # This is the most general rule to limit blank line counts in the output.
            (r'\n(\s*\n){2,}', r'\n\n'),

            # 2. Remove blank lines between consecutive comments (like section headers):
            #    # comment1\n\n# comment2  ->  # comment1\n# comment2
            (r'(\n\s*#.*)\n(\s*\n)(\s*#.*)', r'\1\n\3'),
            
            # 3. Remove blank lines between a closing brace and another closing brace (e.g., end of nested blocks)
            #    }\n\n}  ->  }\n}
            (r'(\n\s*})\n(\s*\n)(\s*})', r'\1\n\3'),
            
            # 4. Remove blank lines between an opening brace and the first child if it's a comment
            #    e.g., { \n # comment  ->  { # comment
            #    This accounts for parsing behavior where a newline might be added before a comment child
            #    inside a block.
            (r'(\{\s*\n)\s*(\s*#.*)', r'\1\2'),
        ]

    def process(self, content_string):
        processed_content = content_string
        for pattern, replacement in self.patterns:
            processed_content = re.sub(pattern, replacement, processed_content, flags=re.MULTILINE)
        return processed_content