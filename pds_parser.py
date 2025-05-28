import re
import copy
import sys
import hashlib
from enum import Enum

# --- Node Base Class and Concrete Node Types ---
class PdsNode:
    def __init__(self, parent=None, line_number=None, original_line_content="", comment_text_on_line=None):
        self.parent = parent
        self.line_number = line_number
        self.original_line_content = original_line_content
        self.comment_text_on_line = comment_text_on_line.strip() if comment_text_on_line else None
        self.children = [] # For PdsBlock
        self.leading_comments_and_blanks = []
        self.diff_path_key_suffix_counter = 0
        self.sim_merge_comment = None # For attaching SimMerge annotations

    def __repr__(self):
        return f"<{self.__class__.__name__} L{self.line_number or '?'}>"

    def add_leading_comment_or_blank(self, node):
        self.leading_comments_and_blanks.append(node)

    def get_structural_components(self, shallow_block=False):
        raise NotImplementedError(f"get_structural_components not implemented for {self.__class__.__name__}")

    def get_diff_key_base(self):
        raise NotImplementedError(f"get_diff_key_base not implemented for {self.__class__.__name__}")

    def to_pds_string(self, indent_level=0, is_list_item=False):
        raise NotImplementedError(f"to_pds_string not implemented for {self.__class__.__name__}")

    def _render_leading_comments_and_blanks(self, indent_level):
        s = []
        for node in self.leading_comments_and_blanks:
            s.append(node.to_pds_string(indent_level))
        return "".join(s)
    
    def _render_eol_comment_with_sim_merge(self):
        comments = []
        if self.comment_text_on_line:
            comments.append(self.comment_text_on_line)
        if self.sim_merge_comment:
            comments.append(self.sim_merge_comment)
        
        return f" # {'; '.join(comments)}" if comments else ""


    def find_child_by_diff_key(self, diff_key_segment):
        # This method is for PdsBlock or nodes that have a concept of named/indexed children
        # PdsList items are handled differently (by index or value usually)
        children_list = []
        if hasattr(self, 'children') and self.children:
            children_list = self.children
        elif hasattr(self, 'values') and self.values and all(isinstance(v, PdsNode) for v in self.values):
             # If PdsList contains PdsNode items (like list of blocks)
            children_list = [v for v in self.values if isinstance(v, PdsNode)]


        if not children_list: return None

        key_base, _, index_str = diff_key_segment.partition('___')
        try:
            target_index = int(index_str)
        except ValueError:
            # print(f"Warning: Invalid index in diff_key_segment '{diff_key_segment}'")
            return None # Or handle as error
        
        current_index = 0
        for child in children_list:
            if child.get_diff_key_base() == key_base:
                if current_index == target_index:
                    return child
                current_index += 1
        return None
    
    def find_child_by_key(self, key_to_find): # Convenience for PdsBlock
        if not hasattr(self, 'children'): return None
        for child in self.children:
            if hasattr(child, 'key') and child.key == key_to_find:
                return child
        return None

class PdsKeyValuePair(PdsNode):
    def __init__(self, key, value, **kwargs):
        super().__init__(**kwargs)
        self.key = key
        self.value = value 

    def __repr__(self):
        val_repr = self.value
        if isinstance(self.value, PdsBlock): val_repr = f"Block({self.value.key or 'Anon'})"
        elif isinstance(self.value, PdsList): val_repr = f"List({self.value.key or 'Anon'})"
        return f"<PdsKeyValuePair L{self.line_number or '?'} {self.key}={val_repr}>"

    def get_structural_components(self, shallow_block=False):
        if isinstance(self.value, PdsNode):
            return (self.key, self.value.get_structural_components(shallow_block=True if isinstance(self.value, PdsBlock) else shallow_block))
        return (self.key, self.value)

    def get_diff_key_base(self):
        return str(self.key)

    def to_pds_string(self, indent_level=0, is_list_item=False):
        indent = "\t" * indent_level
        s = self._render_leading_comments_and_blanks(indent_level)
        
        val_str = ""
        if isinstance(self.value, PdsBlock) or isinstance(self.value, PdsList): 
            # Render the block/list value. It will handle its own key (None) and braces.
            # It needs to be rendered without the parent KVP's indent for its own content lines.
            val_str = self.value.to_pds_string(indent_level if self.value.key else 0, is_list_item=True, is_kvp_value=True).strip()
            # If the block/list string is multi-line, the KVP = needs to be on its own line.
            if '\n' in val_str:
                s += f"{indent}{self.key} ={self._render_eol_comment_with_sim_merge()}\n{indent}{val_str}\n" # Indent the block itself
                return s # Already includes newline
            # else it's single line like key = { val }
        elif isinstance(self.value, bool):
            val_str = "yes" if self.value else "no"
        elif isinstance(self.value, str):
            if not self.value or re.search(r'[\s{}=#>]', self.value) or \
               self.value.lower() in ["yes", "no", "true", "false"] or \
               re.match(r"^-?\d+(\.\d*)?$", self.value): # Is a number-like string
                val_str = f'"{self.value}"'
            else:
                val_str = self.value
        elif self.value is None: # Should not happen for valid KVP
            val_str = '""' 
        else:
            val_str = str(self.value)
        
        s += f"{indent}{self.key} = {val_str}{self._render_eol_comment_with_sim_merge()}\n"
        return s

class PdsList(PdsNode):
    def __init__(self, key=None, **kwargs):
        super().__init__(**kwargs)
        self.key = key 
        self.values = [] # Items: primitives, PdsBlock (keyed or anonymous)

    def __repr__(self):
        return f"<PdsList L{self.line_number or '?'} key='{self.key}' items={len(self.values)}>"

    def get_structural_components(self, shallow_block=False):
        items_struct = []
        for item in self.values:
            if isinstance(item, PdsNode):
                items_struct.append(item.get_structural_components(shallow_block=shallow_block))
            else: 
                items_struct.append(item)
        # For lists, order might matter or not. Diffing logic will decide.
        # For now, return as a tuple (ordered)
        return (self.key, tuple(items_struct))

    def get_diff_key_base(self):
        return str(self.key) if self.key else "__ANONYMOUS_LIST__"

    def to_pds_string(self, indent_level=0, is_list_item=False, is_kvp_value=False):
        indent = "\t" * indent_level
        s = self._render_leading_comments_and_blanks(indent_level)

        is_multiline = '\n' in self.original_line_content or len(self.values) > 3 or \
                       any(isinstance(v, PdsBlock) for v in self.values) or \
                       any(isinstance(v, PdsList) for v in self.values) or \
                       (len(self.values) > 0 and any(getattr(v, 'leading_comments_and_blanks', []) for v in self.values))


        # Determine prefix for `key = {` or just `{`
        list_opener = ""
        if self.key: list_opener = f"{indent}{self.key} = {{ "
        elif not is_kvp_value : list_opener = f"{indent}{{ " # Anonymous list item itself
        else: list_opener = "{ " # Part of KVP value, e.g. key = { item1 }

        if not self.values and not is_multiline: # Empty list, single line: key = { }
            s += list_opener.strip() + " }" + self._render_eol_comment_with_sim_merge() + "\n"
            return s
        
        # If it became multiline due to sim_merge_comment on an item that is PdsComment
        if not is_multiline and any(isinstance(v, PdsComment) and v.sim_merge_comment for v in self.values):
            is_multiline = True


        if not is_multiline: # Single line list: key = { item1 item2 }
            item_strings = []
            for item in self.values:
                if isinstance(item, PdsBlock) or isinstance(item, PdsList): # Anonymous block/list in list
                    item_strings.append(item.to_pds_string(0, is_list_item=True).strip()) 
                elif isinstance(item, PdsComment): # Should not happen for single-line lists usually
                    item_strings.append(f"# {item.comment_text}{item._render_eol_comment_with_sim_merge()}")
                elif isinstance(item, str):
                    if not item or re.search(r'[\s{}=#>]', item) or item.lower() in ["yes", "no", "true", "false"] or re.match(r"^-?\d+(\.\d*)?$", item):
                         item_strings.append(f'"{item}"')
                    else: item_strings.append(item)
                elif isinstance(item, bool):
                    item_strings.append("yes" if item else "no")
                else:
                    item_strings.append(str(item))
            s += list_opener.strip() + " ".join(item_strings) + " }" + self._render_eol_comment_with_sim_merge() + "\n"
        else: # Multi-line list
            s += list_opener.strip() + self._render_eol_comment_with_sim_merge() + "\n"
            for item in self.values:
                item_s = ""
                # Render leading comments/blanks of the item itself if it's a PdsNode
                if isinstance(item, PdsNode):
                    item_s += item._render_leading_comments_and_blanks(indent_level + 1)
                    item_s += item.to_pds_string(indent_level + 1, is_list_item=True) 
                else: # primitive
                    val_str = ""
                    if isinstance(item, bool): val_str = "yes" if item else "no"
                    elif isinstance(item, str):
                        if not item or re.search(r'[\s{}=#>]', item) or item.lower() in ["yes", "no", "true", "false"] or re.match(r"^-?\d+(\.\d*)?$", item): 
                            val_str = f'"{item}"'
                        else: val_str = item
                    elif item is None: val_str = '""' # Should not happen
                    else: val_str = str(item)
                    item_s += f"{indent}\t{val_str}\n" 
                s += item_s
            s += f"{indent}}}\n"
        return s

class PdsBlock(PdsNode):
    def __init__(self, key=None, **kwargs): 
        super().__init__(**kwargs)
        self.key = key
        # self.children is inherited from PdsNode

    def __repr__(self):
        return f"<PdsBlock L{self.line_number or '?'} key='{self.key}' children={len(self.children)}>"
    
    def get_structural_components(self, shallow_block=False):
        if shallow_block:
            return (self.key, f"<Block content {'not inspected' if self.children else 'empty'}>")

        children_structs = []
        temp_child_counters = {}
        for child in self.children:
            child_key_base = child.get_diff_key_base()
            idx = temp_child_counters.get(child_key_base, 0)
            child_diff_key_for_struct = f"{child_key_base}___{idx}"
            temp_child_counters[child_key_base] = idx + 1
            children_structs.append((child_diff_key_for_struct, child.get_structural_components(shallow_block=False)))
        return (self.key, frozenset(children_structs))

    def get_diff_key_base(self):
        return str(self.key) if self.key is not None else "__ANONYMOUS_BLOCK__"

    def to_pds_string(self, indent_level=0, is_list_item=False, is_kvp_value=False):
        indent = "\t" * indent_level
        s = self._render_leading_comments_and_blanks(indent_level)
        
        open_brace_line = ""
        if self.key:
            open_brace_line = f"{indent}{self.key} = {{ {self._render_eol_comment_with_sim_merge()}\n"
        elif not is_kvp_value: 
            open_brace_line = f"{indent}{{ {self._render_eol_comment_with_sim_merge()}\n"
        else: 
             open_brace_line = f"{{ {self._render_eol_comment_with_sim_merge()}" # KVP val, might be single line
             if not self.children: # Empty anonymous block as KVP value: key = { }
                 open_brace_line += " }"
                 return s + open_brace_line # No trailing newline if it's part of KVP line
             open_brace_line += "\n"


        s += open_brace_line

        # Render children
        for child_node in self.children:
            s += child_node.to_pds_string(indent_level + 1)
        
        if not is_kvp_value: 
            s += f"{indent}}}\n"
        else: # Block is a KVP's value
            if '\n' in open_brace_line : # It was multi-line
                 s += f"{indent}}}\n"
            else: # Was single line, e.g. key = { child=val }
                 s += f"}}\n" # This needs careful newline handling with KVP. KVP adds final \n.
    
        return s

class PdsComment(PdsNode):
    def __init__(self, comment_text, **kwargs):
        super().__init__(**kwargs)
        self.comment_text = comment_text.strip() if comment_text else ""

    def __repr__(self):
        return f"<PdsComment L{self.line_number or '?'} text='{self.comment_text[:30]}{'...' if len(self.comment_text)>30 else ''}'>"

    def get_structural_components(self, shallow_block=False):
        return ("__COMMENT__", self.comment_text) 

    def get_diff_key_base(self):
        h = hashlib.md5(self.comment_text.encode('utf-8')).hexdigest()[:10]
        return f"__COMMENT_{h}"

    def to_pds_string(self, indent_level=0, is_list_item=False):
        indent = "\t" * indent_level
        s = self._render_leading_comments_and_blanks(indent_level) 
        s += f"{indent}# {self.comment_text}{self._render_eol_comment_with_sim_merge()}\n"
        return s

class PdsBlankLine(PdsNode):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def __repr__(self):
        return f"<PdsBlankLine L{self.line_number or '?'}>"

    def get_structural_components(self, shallow_block=False):
        return ("__BLANKLINE__", self.line_number) 

    def get_diff_key_base(self):
        return "__BLANKLINE__" # Suffix will differentiate

    def to_pds_string(self, indent_level=0, is_list_item=False):
        s = self._render_leading_comments_and_blanks(indent_level)
        s += "\n" 
        return s

class PdsOperatorCondition(PdsNode):
    def __init__(self, key, operator, value, **kwargs):
        super().__init__(**kwargs)
        self.key = key
        self.operator = operator
        self.value = value

    def __repr__(self):
        return f"<PdsOperatorCondition L{self.line_number or '?'} {self.key} {self.operator} {self.value}>"

    def get_structural_components(self, shallow_block=False):
        return (self.key, self.operator, self.value)

    def get_diff_key_base(self):
        return str(self.key) 

    def to_pds_string(self, indent_level=0, is_list_item=False):
        indent = "\t" * indent_level
        s = self._render_leading_comments_and_blanks(indent_level)
        
        val_str = ""
        if isinstance(self.value, bool): val_str = "yes" if self.value else "no"
        elif isinstance(self.value, str):
            if not self.value or re.search(r'[\s{}=#>]', self.value) or self.value.lower() in ["yes", "no", "true", "false"] or re.match(r"^-?\d+(\.\d*)?$", self.value): 
                val_str = f'"{self.value}"'
            else: val_str = self.value
        elif self.value is None: val_str = '""'
        else: val_str = str(self.value)

        s += f"{indent}{self.key} {self.operator} {val_str}{self._render_eol_comment_with_sim_merge()}\n"
        return s

# --- Parser Class ---
class PdsParser:
    _RE_COMMENT = re.compile(r"^\s*#\s*(.*)")
    _RE_KVP = re.compile(r"""^\s*([a-zA-Z_][\w-]*)\s*=\s*(                           # Key and equals
                                (?:\"((?:\\\"|[^\"])*?)\") |                          # Quoted string value (group 2 is content)
                                (?:(-?\d+\.\d*(?:[eE][-+]?\d+)?|-?\d+[eE][-+]?\d+|-?\.\d+(?:[eE][-+]?\d+)?|-?\d+)) |                        # Number (float or int) (group 3)
                                (yes|no|true|false) |                             # Boolean (group 4)
                                ([^\s\"\{#][^\s\{#=]*(?<!\s))                     # Unquoted string (group 5)
                            )\s*(?:#\s*(.*))?$""", re.VERBOSE | re.IGNORECASE) # Ignore case for yes/no/true/false
    
    _RE_OPERATOR = re.compile(r"""^\s*([a-zA-Z_][\w-]*)\s*([><=!]=?|!=)\s*(             # Key and operator
                                (?:\"((?:\\\"|[^\"])*?)\") |                          # Quoted string value (group 4)
                                (?:(-?\d+\.\d*(?:[eE][-+]?\d+)?|-?\d+[eE][-+]?\d+|-?\.\d+(?:[eE][-+]?\d+)?|-?\d+)) |                        # Number (group 5)
                                (yes|no|true|false) |                             # Boolean (group 6)
                                ([^\s\"\{#][^\s\{#=]*(?<!\s))                     # Unquoted string (group 7)
                            )\s*(?:#\s*(.*))?$""", re.VERBOSE | re.IGNORECASE)
    
    _RE_BLOCK_LIST_START = re.compile(r"^\s*(?:([a-zA-Z_][\w-]*)\s*=\s*)?\{\s*(?:#\s*(.*))?$")
    _RE_BLOCK_LIST_END = re.compile(r"^\s*\}\s*(?:#\s*(.*))?$")
    _RE_SIMPLE_LIST_ITEM = re.compile(r"""(?:\"((?:\\\"|[^\"])*?)\") | (?:(-?\d+\.\d*(?:[eE][-+]?\d+)?|-?\d+[eE][-+]?\d+|-?\.\d+(?:[eE][-+]?\d+)?|-?\d+)) | (yes|no|true|false) | ([a-zA-Z_][\w-]*)""", re.VERBOSE | re.IGNORECASE)


    def parse_file(self, filepath):
        try:
            with open(filepath, 'r', encoding='utf-8-sig') as f: content = f.read()
        except UnicodeDecodeError:
            with open(filepath, 'r', encoding='utf-8') as f: content = f.read()
        except FileNotFoundError:
            # sys.stderr.write(f"PdsParser: File not found: {filepath}\n")
            return []
        return self.parse_content(content)

    def _parse_primitive_value(self, val_str, quoted_str_content, number_str, bool_str, unquoted_str):
        if quoted_str_content is not None: return quoted_str_content.replace('\\"', '"')
        if number_str is not None:
            try: return int(number_str)
            except ValueError: return float(number_str)
        if bool_str is not None: return bool_str.lower() == "yes" or bool_str.lower() == "true"
        if unquoted_str is not None: return unquoted_str
        return val_str # Fallback, should not happen if regex matches one group

    def _peek_next_significant_line_info(self, lines, start_idx, current_block_indent):
        for i in range(start_idx, len(lines)):
            line_content = lines[i]
            stripped_line = line_content.strip()
            
            if not stripped_line or self._RE_COMMENT.match(stripped_line):
                continue # Skip blank or comment lines

            line_indent = len(line_content) - len(line_content.lstrip())
            # If this significant line is less indented or equally indented to the block opener,
            # it means the block is empty or closing.
            if line_indent <= current_block_indent and not self._RE_BLOCK_LIST_END.match(line_content): # not the closing brace itself
                 # this means the block is empty and the next line belongs to parent or is sibling
                 if self._RE_KVP.match(stripped_line) or self._RE_OPERATOR.match(stripped_line):
                     return "EMPTY_LIKELY_BLOCK" # Empty, but if it had content, it'd be KVP/Op like
                 return "EMPTY_LIKELY_LIST"


            # Check line types
            if self._RE_KVP.match(stripped_line): return "KVP"
            if self._RE_OPERATOR.match(stripped_line): return "OPERATOR"
            # If it's another block/list start, it's a list item (keyed or anonymous block)
            if self._RE_BLOCK_LIST_START.match(stripped_line): return "LIST_ITEM_BLOCK" 
            # If it's a simple value, it's a list item
            if self._RE_SIMPLE_LIST_ITEM.fullmatch(stripped_line): return "LIST_ITEM_PRIMITIVE"
            
            # If it's the end of the current block
            if self._RE_BLOCK_LIST_END.match(line_content) and line_indent == current_block_indent:
                return "EMPTY_SCOPE" # Empty scope of determined type by key or default

            return "UNKNOWN_LIST_ITEM" # Fallback for list items not matching simple patterns
        return "EOF" # End of file reached

    def parse_content(self, text_content):
        lines = text_content.splitlines()
        root_nodes = []
        node_stack = [(root_nodes, -1, "ROOT")] # (children_list_or_values_list, indent_level, type_of_container ('BLOCK' or 'LIST'))
        pending_comments_and_blanks = []

        for line_num_0based, original_line in enumerate(lines):
            line_number = line_num_0based + 1
            stripped_line = original_line.strip()
            current_children_list, block_indent_level, container_type = node_stack[-1]
            current_line_indent = len(original_line) - len(original_line.lstrip())

            # Handle Block/List End '}'
            match_block_end = self._RE_BLOCK_LIST_END.match(original_line)
            if match_block_end:
                if current_line_indent == block_indent_level and block_indent_level != -1:
                    eol_comment_end = match_block_end.group(1)
                    if current_children_list is not root_nodes and pending_comments_and_blanks:
                         # Attach pending items as last children of the closing block/list
                        if container_type == "BLOCK":
                            node_stack[-2][0][-1].children.extend(pending_comments_and_blanks)
                        elif container_type == "LIST": # Comments/blanks can be items in list if PdsComment nodes
                            node_stack[-2][0][-1].values.extend(p for p in pending_comments_and_blanks if isinstance(p,PdsComment) or isinstance(p,PdsBlankLine))


                        pending_comments_and_blanks = []
                    
                    # Attach EOL comment on '}' to the block/list node itself if it doesn't have one
                    if current_children_list is not root_nodes: # We are inside a block/list
                        parent_node = node_stack[-2][0][-1] # The block/list we are closing
                        if eol_comment_end and not parent_node.comment_text_on_line:
                            parent_node.comment_text_on_line = eol_comment_end
                        elif eol_comment_end and parent_node.comment_text_on_line and parent_node.comment_text_on_line != eol_comment_end:
                             # Both opener and closer have comments, store closer comment as a child if block
                            if isinstance(parent_node, PdsBlock):
                                 parent_node.children.append(PdsComment(comment_text=eol_comment_end, line_number=line_number, original_line_content=original_line))


                    node_stack.pop()
                    continue

            if not stripped_line:
                pending_comments_and_blanks.append(PdsBlankLine(line_number=line_number, original_line_content=original_line))
                continue
            
            match_comment_line = self._RE_COMMENT.match(stripped_line)
            if match_comment_line:
                pending_comments_and_blanks.append(PdsComment(comment_text=match_comment_line.group(1), line_number=line_number, original_line_content=original_line))
                continue

            # --- Structural Nodes ---
            node = None
            # Attach pending comments/blanks before creating the node
            current_node_leading_comments = pending_comments_and_blanks
            pending_comments_and_blanks = []

            # Try Block/List Start
            match_block_list_start = self._RE_BLOCK_LIST_START.match(stripped_line)
            if match_block_list_start:
                key, eol_comment = match_block_list_start.groups()
                
                # This is a multi-line block/list opener
                # Peek next significant line to determine type
                # Pass current_line_indent of the opening brace's line
                # The children will be at current_line_indent + 1 (typically)
                # The closing brace '}' will be at current_line_indent
                next_line_type = self._peek_next_significant_line_info(lines, line_num_0based + 1, current_line_indent)
                
                if next_line_type in ["KVP", "OPERATOR", "EMPTY_LIKELY_BLOCK"] or (key and next_line_type=="EMPTY_SCOPE"): # Default empty keyed to block
                    node = PdsBlock(key=key, line_number=line_number, original_line_content=original_line, comment_text_on_line=eol_comment)
                    node_stack.append((node.children, current_line_indent, "BLOCK"))
                else: # LIST_ITEM_PRIMITIVE, LIST_ITEM_BLOCK, UNKNOWN_LIST_ITEM, EMPTY_LIKELY_LIST, EOF, or anonymous EMPTY_SCOPE
                    node = PdsList(key=key, line_number=line_number, original_line_content=original_line, comment_text_on_line=eol_comment)
                    node_stack.append((node.values, current_line_indent, "LIST"))
            
            elif container_type == "BLOCK": # Inside a PdsBlock, expect KVP or Operator
                match_op = self._RE_OPERATOR.match(stripped_line)
                if match_op:
                    op_key, operator, _, val_str_q, val_str_n, val_str_b, val_str_u, eol_comment = match_op.groups()
                    value = self._parse_primitive_value(None, val_str_q, val_str_n, val_str_b, val_str_u)
                    node = PdsOperatorCondition(key=op_key, operator=operator, value=value, line_number=line_number, original_line_content=original_line, comment_text_on_line=eol_comment)
                else: # Must be KVP if in a block
                    match_kvp = self._RE_KVP.match(stripped_line)
                    if match_kvp:
                        kvp_key, _, val_str_q, val_str_n, val_str_b, val_str_u, eol_comment = match_kvp.groups()
                        value = self._parse_primitive_value(None, val_str_q, val_str_n, val_str_b, val_str_u)
                        node = PdsKeyValuePair(key=kvp_key, value=value, line_number=line_number, original_line_content=original_line, comment_text_on_line=eol_comment)
            
            elif container_type == "LIST": # Inside a PdsList, expect list items
                # Item can be a primitive, or a new PdsBlock (keyed or anonymous)
                # A PdsBlock item would have been matched by _RE_BLOCK_LIST_START already and node created.
                # So if node is still None here, it must be a primitive list item.
                match_simple_item = self._RE_SIMPLE_LIST_ITEM.fullmatch(stripped_line)
                if match_simple_item:
                    _,val_str_q, val_str_n, val_str_b, val_str_u = match_simple_item.groups() # _RE_SIMPLE_LIST_ITEM has initial non-capturing group
                    item_value = self._parse_primitive_value(None,val_str_q, val_str_n, val_str_b, val_str_u)
                    # Primitives are added directly to PdsList.values
                    # We need to handle comments for primitive items if they are significant
                    if current_node_leading_comments:
                        # If primitive items have leading comments, they become PdsComment items in the list too
                        current_children_list.extend(current_node_leading_comments)
                        current_node_leading_comments = []
                    current_children_list.append(item_value)
                    # This primitive is not a PdsNode, so skip node processing below
                    # This part is problematic if primitive items are expected to hold comments.
                    # For this benchmark, list item comments are not explicitly tested.
                    # If a PdsComment node was parsed and is pending, it will be added to list.
                    continue 
                # else: Unparsed list item, error or more complex item type
            
            else: # ROOT level, expect KVP, Operator, Block, List
                match_op = self._RE_OPERATOR.match(stripped_line)
                if match_op:
                    op_key, operator, _, val_str_q, val_str_n, val_str_b, val_str_u, eol_comment = match_op.groups()
                    value = self._parse_primitive_value(None, val_str_q, val_str_n, val_str_b, val_str_u)
                    node = PdsOperatorCondition(key=op_key, operator=operator, value=value, line_number=line_number, original_line_content=original_line, comment_text_on_line=eol_comment)
                else: # Default to KVP at root if not block/list start or operator
                    match_kvp = self._RE_KVP.match(stripped_line)
                    if match_kvp:
                        kvp_key, _, val_str_q, val_str_n, val_str_b, val_str_u, eol_comment = match_kvp.groups()
                        value = self._parse_primitive_value(None, val_str_q, val_str_n, val_str_b, val_str_u)
                        node = PdsKeyValuePair(key=kvp_key, value=value, line_number=line_number, original_line_content=original_line, comment_text_on_line=eol_comment)

            if node:
                node.leading_comments_and_blanks = current_node_leading_comments
                current_children_list.append(node)
            elif stripped_line: # Unparsed line
                # This might be a primitive list item if the parent is a list, but that case should be handled above.
                print(f"PdsParser WARNING L{line_number}: Unparsed line in {container_type} context: '{original_line}'")
                # Fallback: treat as a comment if it seems like it, or a string KVP if desperate
                if current_node_leading_comments: # Attach to last real node or root
                    if current_children_list:
                         if isinstance(current_children_list[-1], PdsNode):
                            current_children_list[-1].children.extend(current_node_leading_comments) # Bad place
                    else:
                        root_nodes.extend(current_node_leading_comments)


        if pending_comments_and_blanks:
            root_nodes.extend(pending_comments_and_blanks)
            
        if len(node_stack) > 1:
            print(f"PdsParser WARNING: Unclosed blocks at end of file. Stack size: {len(node_stack)}")

        self._assign_diff_key_suffixes(root_nodes)
        return root_nodes

    def _assign_diff_key_suffixes(self, nodes_list):
        if not nodes_list: return
        key_counts = {}
        for node in nodes_list:
            if not isinstance(node, PdsNode): continue # Skip primitives in lists

            base_key = node.get_diff_key_base()
            idx = key_counts.get(base_key, 0)
            node.diff_path_key_suffix_counter = idx 
            key_counts[base_key] = idx + 1

            if isinstance(node, PdsBlock):
                self._assign_diff_key_suffixes(node.children)
            elif isinstance(node, PdsList):
                # For PdsList, its items that are PdsNodes also need suffixes within the list's "namespace"
                node_items_in_list = [v for v in node.values if isinstance(v, PdsNode)]
                if node_items_in_list:
                    self._assign_diff_key_suffixes(node_items_in_list)
            elif isinstance(node, PdsKeyValuePair) and isinstance(node.value, (PdsBlock, PdsList)):
                # If KVP's value is a block/list, recurse (though it's anonymous, so base key is fixed)
                 self._assign_diff_key_suffixes([node.value])


    @staticmethod
    def _nodes_to_string(nodes_list, indent_level=0):
        s = []
        for node in nodes_list:
            if isinstance(node, PdsNode):
                 s.append(node.to_pds_string(indent_level))
            else: # Primitive list item
                 # This should be handled by PdsList.to_pds_string ideally
                 s.append(("\t" * indent_level) + str(node) + "\n") 
        return "".join(s)

def get_node_diff_key_for_find(node: PdsNode):
    if not isinstance(node, PdsNode):
        # Handle cases where node might be primitive (e.g. list item not wrapped)
        # This indicates an issue in how nodes are passed or if primitives are expected
        # For benchmark, it expects PdsNode.
        raise TypeError(f"Expected PdsNode, got {type(node)} with value {node!r}")
    
    suffix = getattr(node, 'diff_path_key_suffix_counter', 0) 
    return f"{node.get_diff_key_base()}___{suffix}"