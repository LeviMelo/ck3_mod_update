import re

class PdsPostProcessor:
    """
    Applies regex-based formatting adjustments to PDS script content.
    Intended to be run AFTER parsing and reconstruction, to normalize newlines.
    """
    def __init__(self):
        # List of regex patterns and their replacements.
        # Order matters: more general patterns or patterns that create new valid contexts should come first.
        self.patterns = [
            # 1. Remove blank lines immediately after an opening brace if followed by content/comment
            #    `{\n\n    content` -> `{\n    content`
            #    `{\n\n# comment` -> `{\n# comment`
            (r'({\s*\n)\s*\n(\s*[#a-zA-Z0-9])', r'\1\2'), # Capture initial indent, then content/comment
            (r'({\s*\n)\s*\n(\s*#)', r'\1\2'), # Specific for comment after {
            
            # 2. Remove blank lines immediately before a closing brace
            #    `    content\n\n}` -> `    content\n}`
            #    `# comment\n\n}` -> `# comment\n}`
            (r'(\S)\n\s*\n(\s*})', r'\1\n\2'), # If content/comment preceded the blank line

            # 3. Remove blank lines between consecutive comments (### comment\n\n### comment)
            #    This is for "section breaks" that are consecutive but parser adds a blank line.
            #    Captures indent before #.
            (r'(\n\s*#.*?)\n(\s*\n)(\s*#)', r'\1\n\3'), # Matches `line1\n\nline2` where line1/line2 are comments

            # 4. Remove blank lines between consecutive closing braces `}\n\n}` -> `}\n}`
            #    Captures indent before second }
            (r'(\n\s*})\n(\s*\n)(\s*})', r'\1\n\3'),
            
            # 5. Remove blank lines between consecutive opening braces `{\n\n{` -> `{\n{`
            (r'(\n\s*{\s*)\n(\s*{\s*)', r'\1\2'),

            # 6. Remove blank lines between content and comments on the next line if the original was compact
            #    `val\n\n# comment` -> `val\n# comment` (less common but possible)
            (r'([a-zA-Z0-9"\'])\n\s*\n(\s*#)', r'\1\n\2'),

            # 7. Normalize multiple blank lines to a single blank line
            #    `\n\n\n` -> `\n\n`
            (r'\n\s*\n\s*\n', r'\n\n'),
        ]

    def process(self, content_string):
        processed_content = content_string
        for pattern, replacement in self.patterns:
            processed_content = re.sub(pattern, replacement, processed_content, flags=re.MULTILINE)
        
        # Final pass for any remaining leading/trailing newlines that aren't strict part of file content
        # processed_content = processed_content.strip() # Might be too aggressive for final output
        # If it's a file ending with just \n\n, strip() would turn it into one \n, but that's fine.
        return processed_content