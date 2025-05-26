import re
import os
import difflib

class PdsNode:
    """Base class for all elements in the PDS script tree."""
    def __init__(self, raw_line, indent_level=0, line_number=-1):
        self.raw_line = raw_line 
        self.indent_level = indent_level
        self.comment_text_on_line = None # Stores only the text part of a comment on this line
        self.line_number = line_number

    def to_string(self, current_indent=0): # Used by PdsComment, PdsBlankLine
        return self.raw_line # Assumes raw_line includes its newline

    def __repr__(self):
        key_repr = getattr(self, 'key', 'N/A')
        comment_repr = getattr(self, 'comment_text_on_line', None)
        comment_str = f", CommentText='{comment_repr[:20]}...'" if comment_repr else ""
        
        if isinstance(self, PdsComment):
             # For PdsComment, key_repr shows its actual comment_text for clarity
             key_repr = f"Comment: '{getattr(self, 'comment_text', '')[:30]}...'"
        elif isinstance(self, PdsBlankLine):
             key_repr = "Blank Line"
        return f"<{self.__class__.__name__} L{self.line_number} C{self.indent_level + 1} Key='{key_repr}'{comment_str}>"

class PdsComment(PdsNode): # Represents a line that IS a comment
    def __init__(self, raw_line, indent_level=0, line_number=-1, comment_text=""):
        super().__init__(raw_line, indent_level, line_number)
        self.comment_text = comment_text # Just the text content, not including '#'

class PdsBlankLine(PdsNode):
    def __init__(self, raw_line, indent_level=0, line_number=-1):
        super().__init__(raw_line, indent_level, line_number)

class PdsKeyValuePair(PdsNode):
    def __init__(self, key, value, raw_line_ref, indent_level=0, comment_text=None, line_number=-1):
        super().__init__(raw_line_ref, indent_level, line_number)
        self.key = key
        self.value = value 
        self.comment_text_on_line = comment_text

    def to_string(self, current_indent=0):
        indent_str = " " * current_indent
        value_str = str(self.value) 
        if self.comment_text_on_line:
            return f"{indent_str}{self.key} = {value_str} # {self.comment_text_on_line}\n"
        return f"{indent_str}{self.key} = {value_str}\n"

class PdsList(PdsNode): 
    def __init__(self, key, values, raw_line_ref, indent_level=0, comment_text=None, line_number=-1):
        super().__init__(raw_line_ref, indent_level, line_number)
        self.key = key
        self.values = values 
        self.comment_text_on_line = comment_text

    def to_string(self, current_indent=0):
        indent_str = " " * current_indent
        value_str = " ".join(self.values)
        inner_content = f" {value_str} " if self.values else " " 
        base_string = f"{indent_str}{self.key} = {{{inner_content}}}"
        if self.comment_text_on_line:
            return f"{base_string} # {self.comment_text_on_line}\n"
        return f"{base_string}\n"

class PdsBlock(PdsNode):
    def __init__(self, key, raw_line_ref, indent_level=0, comment_text=None, line_number=-1):
        super().__init__(raw_line_ref, indent_level, line_number)
        self.key = key
        self.children = []
        self.comment_text_on_line = comment_text # Just the text, not '#'

    def add_child(self, node):
        self.children.append(node)

    def to_string(self, current_indent=0):
        indent_str = " " * current_indent
        child_indent = current_indent + 4 # Standard child indent for new/reformatted content
        output_lines = []

        open_brace_line = f"{indent_str}{self.key} = {{" 
        if self.comment_text_on_line:
            open_brace_line += f" # {self.comment_text_on_line}" 
        output_lines.append(open_brace_line + "\n")

        for child in self.children:
            if isinstance(child, (PdsComment, PdsBlankLine)):
                # Preserve raw line for comments/blanks within blocks to maintain original indent & format
                output_lines.append(child.raw_line)
            elif isinstance(child, (PdsBlock, PdsKeyValuePair, PdsList)): # Other structural nodes
                output_lines.append(child.to_string(child_indent))
            else: # Fallback for any other PdsNode types (e.g. if we added raw '}' lines)
                  # This case should ideally not be hit with current parser logic for '}'
                output_lines.append(child.to_string(child_indent if hasattr(child, 'key') else current_indent))
        
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
                    if isinstance(node, PdsBlock):
                        found_node_for_segment = node
                        break
                    return None 
            if found_node_for_segment:
                current_nodes_to_search = found_node_for_segment.children
            else: return None
        return None

class PdsParser:
    def __init__(self):
        self.root_nodes = []
        self.line_counter = 0

    def parse_lines(self, lines_with_newlines):
        self.root_nodes = []
        self.line_counter = 0
        stack = []

        # Regexes: Comment groups capture only text *after* "#" and optional spaces
        FULL_LINE_COMMENT_REGEX = re.compile(r"^\s*#\s*(.*)$") # Group 1: comment text
        BLANK_LINE_REGEX = re.compile(r"^\s*$")
        # Group 1 (key), Group 2 (inner_content_raw), Group 3 (comment_text after #)
        SINGLE_LINE_BLOCK_OR_LIST_REGEX = re.compile(r"^\s*([\w\s\.:@\-]+)\s*=\s*\{([^\}]*?)\}\s*(?:#\s*(.*))?$")
        BLOCK_OPEN_REGEX = re.compile(r"^\s*([\w\s\.:@\-]+)\s*=\s*\{\s*(?:#\s*(.*))?$") # Group 2: comment text
        KEY_VALUE_PAIR_REGEX = re.compile(r"^\s*([\w\s\.:@\-]+)\s*=\s*([^#]*?)\s*(?:#\s*(.*))?$") # Group 3: comment text

        for i, raw_line_with_newline in enumerate(lines_with_newlines):
            self.line_counter = i + 1
            line = raw_line_with_newline.rstrip('\r\n')
            stripped_line_for_brace_check = line.strip()
            indent_level = len(raw_line_with_newline) - len(raw_line_with_newline.lstrip(' '))
            current_parent = stack[-1] if stack else None

            def get_comment_text_from_match(match_obj, group_idx):
                if match_obj and group_idx <= len(match_obj.groups()) and match_obj.group(group_idx) is not None:
                    return match_obj.group(group_idx).strip() 
                return None

            # Rule A: Closing brace `}`
            if stripped_line_for_brace_check == "}":
                if stack:
                    stack.pop() 
                else:
                    print(f"Warning (L{self.line_counter}): Unexpected closing brace (stack empty): '{line}'")
                continue 

            # Rule B: Blank Lines
            if BLANK_LINE_REGEX.match(line):
                node = PdsBlankLine(raw_line_with_newline, indent_level, self.line_counter)
                if current_parent: current_parent.add_child(node)
                else: self.root_nodes.append(node)
                continue
            
            # Rule C: Full-Line Comments
            match_full_comment = FULL_LINE_COMMENT_REGEX.match(line)
            if match_full_comment:
                comment_text = match_full_comment.group(1).strip()
                node = PdsComment(raw_line_with_newline, indent_level, self.line_counter, comment_text)
                if current_parent: current_parent.add_child(node)
                else: self.root_nodes.append(node)
                continue
            
            # Rule D: Single-line blocks/lists: key = { ... }
            match_single_line = SINGLE_LINE_BLOCK_OR_LIST_REGEX.match(line)
            if match_single_line:
                key = match_single_line.group(1).strip()
                inner_content_raw = match_single_line.group(2) # Group 2 is raw content between {}
                comment_text = get_comment_text_from_match(match_single_line, 3)
                if '=' in inner_content_raw or ':' in inner_content_raw: 
                    node_value = f"{{{inner_content_raw}}}" 
                    node = PdsKeyValuePair(key, node_value, raw_line_with_newline, indent_level, comment_text, self.line_counter)
                else:
                    values = inner_content_raw.strip().split() if inner_content_raw.strip() else []
                    node = PdsList(key, values, raw_line_with_newline, indent_level, comment_text, self.line_counter)
                if current_parent: current_parent.add_child(node)
                else: self.root_nodes.append(node)
                continue

            # Rule E: Multi-line block start: key = {
            match_block_open = BLOCK_OPEN_REGEX.match(line)
            if match_block_open:
                key = match_block_open.group(1).strip()
                comment_text = get_comment_text_from_match(match_block_open, 2)
                new_block = PdsBlock(key, raw_line_with_newline, indent_level, comment_text, self.line_counter)
                if current_parent: current_parent.add_child(new_block)
                else: self.root_nodes.append(new_block)
                stack.append(new_block)
                continue

            # Rule F: Key = Value pair
            match_key_value = KEY_VALUE_PAIR_REGEX.match(line)
            if match_key_value:
                key = match_key_value.group(1).strip()
                value = match_key_value.group(2).strip() 
                comment_text = get_comment_text_from_match(match_key_value, 3)
                node = PdsKeyValuePair(key, value, raw_line_with_newline, indent_level, comment_text, self.line_counter)
                if current_parent: current_parent.add_child(node)
                else: self.root_nodes.append(node)
                continue
            print(f"Warning (L{self.line_counter}): Unhandled line: '{line}'")

    def parse_file(self, filepath):
        if not os.path.exists(filepath): print(f"Error: File not found: {filepath}"); return []
        try:
            with open(filepath, 'r', encoding='utf-8-sig') as f: lines_with_newlines = f.readlines()
        except UnicodeDecodeError:
            try: 
                with open(filepath, 'r', encoding='utf-8') as f: lines_with_newlines = f.readlines()
            except Exception as e_inner: print(f"Error reading file {filepath} with utf-8 fallback: {e_inner}"); return []
        except Exception as e: print(f"Error reading file {filepath}: {e}"); return []
        self.parse_lines(lines_with_newlines)
        return self.root_nodes

    def to_string(self):
        output_lines = []
        for node in self.root_nodes:
            output_lines.append(node.to_string(node.indent_level))
        return "".join(output_lines)