import re
import os
import difflib

class PdsNode:
    """Base class for all elements in the PDS script tree."""
    def __init__(self, raw_line, indent_level=0, line_number=-1, comment_text_on_line=None):
        self.raw_line = raw_line 
        self.indent_level = indent_level
        self.comment_text_on_line = comment_text_on_line
        self.line_number = line_number

    def to_string(self, current_indent=0):
        return self.raw_line

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
        
        return f"<{self.__class__.__name__} L{self.line_number} I{self.indent_level} K='{key_repr}'{comment_str}>"

    def copy(self):
        return self.__class__(
            raw_line=self.raw_line,
            indent_level=self.indent_level,
            line_number=self.line_number,
            comment_text_on_line=self.comment_text_on_line
        )

class PdsComment(PdsNode):
    def __init__(self, raw_line, indent_level=0, line_number=-1, comment_text="", comment_text_on_line=None):
        super().__init__(raw_line, indent_level, line_number, comment_text_on_line=comment_text)
        self.comment_text = comment_text

    def copy(self):
        return self.__class__(
            raw_line=self.raw_line,
            indent_level=self.indent_level,
            line_number=self.line_number,
            comment_text=self.comment_text,
            comment_text_on_line=self.comment_text_on_line 
        )

class PdsBlankLine(PdsNode):
    def __init__(self, raw_line, indent_level=0, line_number=-1, comment_text_on_line=None):
        super().__init__(raw_line, indent_level, line_number, comment_text_on_line)

    def copy(self):
        return self.__class__(
            raw_line=self.raw_line,
            indent_level=self.indent_level,
            line_number=self.line_number,
            comment_text_on_line=self.comment_text_on_line
        )


class PdsKeyValuePair(PdsNode):
    def __init__(self, key, value, raw_line_ref, indent_level=0, comment_text=None, line_number=-1):
        super().__init__(raw_line=raw_line_ref, indent_level=indent_level, 
                         line_number=line_number, comment_text_on_line=comment_text)
        self.key = key
        self.value = value 

    def to_string(self, current_indent=0):
        indent_str = " " * current_indent
        value_str = str(self.value) 
        if self.comment_text_on_line:
            return f"{indent_str}{self.key} = {value_str} # {self.comment_text_on_line}\n"
        return f"{indent_str}{self.key} = {value_str}\n"

    def copy(self):
        return self.__class__(
            key=self.key,
            value=self.value,
            raw_line_ref=self.raw_line,
            indent_level=self.indent_level,
            line_number=self.line_number,
            comment_text=self.comment_text_on_line
        )


class PdsList(PdsNode): 
    def __init__(self, key, values, raw_line_ref, indent_level=0, comment_text=None, line_number=-1):
        super().__init__(raw_line=raw_line_ref, indent_level=indent_level, 
                         line_number=line_number, comment_text_on_line=comment_text)
        self.key = key
        self.values = values 

    def to_string(self, current_indent=0):
        indent_str = " " * current_indent
        value_str = " ".join(self.values)
        inner_content = f" {value_str} " if self.values else " " 
        base_string = f"{indent_str}{self.key} = {{{inner_content}}}"
        if self.comment_text_on_line:
            return f"{base_string} # {self.comment_text_on_line}\n"
        return f"{base_string}\n"

    def copy(self):
        return self.__class__(
            key=self.key,
            values=list(self.values), 
            raw_line_ref=self.raw_line,
            indent_level=self.indent_level,
            line_number=self.line_number,
            comment_text=self.comment_text_on_line
        )

class PdsOperatorCondition(PdsNode):
    """Represents a statement like 'count >= 1' or 'owner ?= this'.
       The operator is not always '=' and it does not define a block.
    """
    def __init__(self, key, operator, value, raw_line_ref, indent_level=0, comment_text=None, line_number=-1):
        super().__init__(raw_line=raw_line_ref, indent_level=indent_level, 
                         line_number=line_number, comment_text_on_line=comment_text)
        self.key = key
        self.operator = operator 
        self.value = value       

    def to_string(self, current_indent=0):
        indent_str = " " * current_indent
        line = f"{indent_str}{self.key} {self.operator} {self.value}"
        if self.comment_text_on_line:
            return f"{line} # {self.comment_text_on_line}\n"
        return f"{line}\n"

    def copy(self):
        return self.__class__(
            key=self.key,
            operator=self.operator,
            value=self.value,
            raw_line_ref=self.raw_line,
            indent_level=self.indent_level,
            line_number=self.line_number,
            comment_text=self.comment_text_on_line
        )


class PdsBlock(PdsNode):
    def __init__(self, key, raw_line_ref, indent_level=0, comment_text=None, line_number=-1):
        super().__init__(raw_line=raw_line_ref, indent_level=indent_level, 
                         line_number=line_number, comment_text_on_line=comment_text)
        self.key = key # This key now includes the operator part if it exists (e.g. "AND =", "owner ?=")
        self.children = []
        # comment_text_on_line is already set by super().__init__

    def add_child(self, node):
        self.children.append(node)

    def to_string(self, current_indent=0):
        indent_str = " " * current_indent
        child_indent = current_indent + 4 
        output_lines = []

        # The key already includes the operator and "=", e.g., "AND =" or "owner ?="
        # So we just print the key and then "{".
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
                # Check for any node type with a 'key' attribute that matches the segment
                if hasattr(node, 'key') and node.key == segment: 
                    if i == len(key_path) - 1: return node # Found the target node
                    elif isinstance(node, PdsBlock): # Can recurse into this block
                        found_node_for_segment = node
                        break # Break inner loop, continue search in outer loop's next segment
                    else: # Segment matches key, but it's not a block and not the final node, path broken
                        return None 
            
            if found_node_for_segment: 
                current_nodes_to_search = found_node_for_segment.children
            else: 
                return None # Segment not found at this level
        return None 

    def copy(self):
        new_node = self.__class__(
            key=self.key,
            raw_line_ref=self.raw_line,
            indent_level=self.indent_level,
            line_number=self.line_number,
            comment_text=self.comment_text_on_line
        )
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
        self.root_nodes = []
        self.line_counter = 0

    def parse_lines(self, lines_with_newlines):
        self.root_nodes = []
        self.line_counter = 0
        stack = []

        # Define all regex patterns. Order of checks in the loop is CRITICAL.

        FULL_LINE_COMMENT_REGEX = re.compile(r"^\s*#\s*(.*)$")
        BLANK_LINE_REGEX = re.compile(r"^\s*$")
        
        # Single-line blocks/lists: key = { ... }
        # Group 1: Key
        # Group 2: Raw content inside braces
        # Group 3: Optional comment
        SINGLE_LINE_BLOCK_OR_LIST_REGEX = re.compile(r"^\s*([\w\s\.:@\-]+?)\s*=\s*\{([^\}]*?)\}\s*(?:#\s*(.*))?$")
        
        # Multi-line block start: `key = {` OR `key OP {` (e.g. `AND = {`, `owner ?= {`, `NOT = {`)
        # Group 1: The entire key-like part including words and an optional operator and the '=' before '{'
        #          e.g. "AND =", "owner ?=", "some_block ="
        # Group 2 (optional): Comment
        BLOCK_OPEN_GENERIC_REGEX = re.compile(r"^\s*([\w\s\.:@\-]+?\s*(?:[=\?\!><]=?|==|!=)?)\s*\{\s*(?:#\s*(.*))?$")
        # Added (?:[=\?\!><]=?|==|!=)? to the key part to allow the operator to be part of the key.
        # This captures "AND =", "owner ?=", etc. including the operator and the ' '.


        # Key Operator Value (e.g., `count >= 1`, `owner ?= this`, `diplomacy = 5`)
        # This is the most general pattern for lines with a key, an operator, and a value.
        # It MUST be checked *after* single-line blocks and multi-line blocks.
        # Group 1: Key (flexible, non-greedy)
        # Group 2: Operator (=, ?=, >, <, >=, <=, !, !=, ==) - must be present and not followed by {
        # Group 3: Value (anything not #, {, } - non-greedy)
        # Group 4: Optional comment
        KEY_OPERATOR_VALUE_GENERAL_REGEX = re.compile(r"^\s*([\w\s\.:@\-]+?)\s*([=\?\!><]=?|==|!=)\s*([^#\{\}]+?)\s*(?:#\s*(.*))?$")


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

            # --- Parsing Rule Priority (from most specific structural to most general) ---

            # Rule A: Closing brace `}` (Always highest priority for structural parsing)
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
            
            # Rule D: Single-line blocks/lists: key = { ... } (e.g. `some_list = { item1 item2 }`)
            match_single_line = SINGLE_LINE_BLOCK_OR_LIST_REGEX.match(line)
            if match_single_line:
                key = match_single_line.group(1).strip()
                inner_content_raw = match_single_line.group(2) 
                comment_text = get_comment_text_from_match(match_single_line, 3)
                
                # Heuristic: If inner_content contains any operator or assignment, treat as complex KV
                if re.search(r'[\w\s\.:@\-]+?\s*([=\?\!><]=?|==|!=)\s*[^#\{\}]+?', inner_content_raw): 
                    node_value = f"{{{inner_content_raw}}}" 
                    node = PdsKeyValuePair(key, node_value, raw_line_with_newline, indent_level, comment_text, self.line_counter)
                else: 
                    values = inner_content_raw.strip().split() if inner_content_raw.strip() else []
                    node = PdsList(key, values, raw_line_with_newline, indent_level, comment_text, self.line_counter)
                
                if current_parent: current_parent.add_child(node)
                else: self.root_nodes.append(node)
                continue

            # Rule E: Multi-line block start: `key = {` OR `key OP {` (e.g. `AND = {`, `owner ?= {`)
            # This must be checked before KEY_OPERATOR_VALUE_GENERAL_REGEX to prioritize blocks.
            match_block_open = BLOCK_OPEN_GENERIC_REGEX.match(line)
            if match_block_open:
                block_key = match_block_open.group(1).strip() # Capture the full key part before '{'
                comment_text = get_comment_text_from_match(match_block_open, 2) 

                new_block = PdsBlock(block_key, raw_line_with_newline, indent_level, comment_text, self.line_counter)
                if current_parent: current_parent.add_child(new_block)
                else: self.root_nodes.append(new_block)
                stack.append(new_block)
                continue

            # Rule F: Key Operator Value (e.g., `count >= 1`, `owner ?= this`, `diplomacy = 5`, `always = yes`)
            # This is the most general pattern for lines with a key, an operator, and a value.
            # It must be AFTER block open rules.
            match_key_op_val = KEY_OPERATOR_VALUE_GENERAL_REGEX.match(line)
            if match_key_op_val:
                key = match_key_op_val.group(1).strip()
                operator = match_key_op_val.group(2).strip()
                value = match_key_op_val.group(3).strip() 
                comment_text = get_comment_text_from_match(match_key_op_val, 4)

                # If the operator is exactly '=', it's a PdsKeyValuePair.
                # Otherwise (e.g., '>=', '!=', '?='), it's a PdsOperatorCondition.
                if operator == '=':
                    node = PdsKeyValuePair(key, value, raw_line_with_newline, indent_level, comment_text, self.line_counter)
                else:
                    node = PdsOperatorCondition(key, operator, value, raw_line_with_newline, indent_level, comment_text, self.line_counter)
                
                if current_parent: current_parent.add_child(node)
                else: self.root_nodes.append(node)
                continue
            
            # If we reach here, it's an unhandled line type
            print(f"Warning (L{self.line_counter}): Unhandled line: '{line}'")

    def parse_file(self, filepath):
        if not os.path.exists(filepath):
            print(f"Error: File not found: {filepath}"); 
            return []
        try:
            with open(filepath, 'r', encoding='utf-8-sig') as f: lines_with_newlines = f.readlines()
        except UnicodeDecodeError:
            try: 
                with open(filepath, 'r', encoding='utf-8') as f: lines_with_newlines = f.readlines()
            except Exception as e_inner: 
                print(f"Error reading file {filepath} with utf-8 fallback: {e_inner}"); 
                return []
        except Exception as e: 
            print(f"Error reading file {filepath}: {e}"); 
            return []
        self.parse_lines(lines_with_newlines)
        return self.root_nodes

    @staticmethod
    def _nodes_to_string(nodes_list):
        output = []
        for node in nodes_list:
            output.append(node.to_string(node.indent_level)) 
        return "".join(output)

    def to_string(self):
        return PdsParser._nodes_to_string(self.root_nodes)