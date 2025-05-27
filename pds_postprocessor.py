import re

class PdsPostProcessor:
    """
    Applies regex-based formatting adjustments to PDS script content.
    Intended to be run AFTER parsing and reconstruction, to normalize newlines
    and other cosmetic whitespace.
    """
    def __init__(self):
        # List of regex patterns and their replacements.
        # Order matters: more specific patterns should often come before more general ones.
        self.patterns = [
            # --- Comment Formatting ---
            # 1. Ensure exactly one space after '#' if comment text follows,
            #    unless the original was '#text' (no space) or '#  text' (multiple spaces).
            #    This is a bit delicate. The goal is to fix '# text' from '#text' if the parser
            #    removed an original space, or to fix '#  text' if the parser added an extra one.
            #    A simpler approach for post-processing is to standardize:
            #    '#text' -> '# text' (add space if missing and not followed by space)
            #    '#  text' -> '# text' (reduce multiple spaces to one)
            #    Let's try to ensure one space if there's content, unless it's a "header" like ###
            (r'^(#)([^#\s\n])', r'\1 \2'),  # Add space: #text -> # text
            (r'^(#) {2,}([^#\n])', r'\1 \2'), # Reduce multiple spaces: #  text -> # text

            # --- Operator and Inline Block Spacing ---
            # 2. Normalize space around operators followed by an opening brace (inline blocks):
            #    e.g., 'key ?=   {' -> 'key ?= {'
            #    e.g., 'key =   {' -> 'key = {'
            #    This targets operators/equals, any number of spaces, then an opening brace.
            #    Captures: (key) (operator_or_equals) (spaces) ({)
            (r'([\w.:@\-]+\s*(?:>=|<=|==|!=|\?=|>|<|=|!)s*)\s+(\{)', r'\1 {'),

            # 3. Normalize space within an inline block: '{ child_content }' -> '{ child_content }'
            #    e.g. '{   key=val   }' -> '{ key=val }'
            #    This is for blocks rendered inline like: `some_key = { child_key = child_val }`
            #    Looks for '{', optional spaces, content, optional spaces, '}'
            #    This needs to be careful not to affect multi-line blocks.
            #    We'll target lines that look like `... = { ... }` or `... operator { ... }`
            #    and normalize the spaces inside the braces on that *single line*.
            (r'(\{)\s+(.*?[^\s])\s+(\})', r'\1 \2 \3'), # Ensures one space padding inside {} for inline blocks

            # --- General Whitespace Normalization ---
            # 4. Normalize multiple blank lines to a single blank line (any sequence of 3+ newlines becomes 2)
            (r'\n(\s*\n){2,}', r'\n\n'),

            # 5. Remove blank lines between consecutive comments (like section headers):
            #    # comment1\n\n# comment2  ->  # comment1\n# comment2
            (r'(\n\s*#.*)\n(\s*\n)(\s*#.*)', r'\1\n\3'),
            
            # 6. Remove blank lines between a closing brace and another closing brace (e.g., end of nested blocks)
            #    }\n\n}  ->  }\n}
            (r'(\n\s*})\n(\s*\n)(\s*})', r'\1\n\3'),
            
            # 7. Remove blank lines between an opening brace and the first child if it's a comment
            #    e.g., { \n # comment  ->  { # comment
            (r'(\{\s*\n)\s*(\s*#.*)', r'\1\2'),
            
            # 8. Remove trailing whitespace from all lines
            (r'[ \t]+$', r''),
        ]

    def process(self, content_string):
        processed_content = content_string
        # It's often beneficial to run general normalizations multiple times or in a specific order
        for _ in range(2): # Run twice to catch cascading changes
            for pattern, replacement in self.patterns:
                processed_content = re.sub(pattern, replacement, processed_content, flags=re.MULTILINE)
        
        # One final pass for specific tricky cases or overall cleanup
        # Remove trailing whitespace again
        processed_content = re.sub(r'[ \t]+$', r'', processed_content, flags=re.MULTILINE)
        # Ensure file ends with a single newline if it has content, or is empty if no content
        if processed_content.strip():
            processed_content = processed_content.rstrip() + "\n"
        else:
            processed_content = ""

        return processed_content