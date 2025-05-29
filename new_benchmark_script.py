import os
import sys
import difflib
import collections # Your script uses this
import re

# --- Python Module Imports ---
# These .py files (pds_parser.py, pds_differ.py)
# are expected to be in the SAME DIRECTORY as this notebook.

# 1. Import the actual node classes directly from pds_parser.py
#    (PdsNode definitions are now in pds_parser.py)
from pds_parser import (
    PdsNode, PdsKeyValuePair, PdsBlock, PdsList,
    PdsComment, PdsBlankLine, PdsOperatorCondition
)
# 2. Import the PdsParser class from pds_parser.py
from pds_parser import PdsParser
# 3. Import necessary components from pds_differ.py
#    (The helper functions _find_node_by_diff_path_in_tree and get_node_diff_key_for_find are now here)
from pds_differ import (
    PdsDiffer, PdsChange, g_subsumed_mod_block_paths_for_simulation,
    _find_node_by_diff_path_in_tree, get_node_diff_key_for_find
)

# --- Confirmation Prints (Optional but helpful) ---
print(f"--- Successfully imported PdsNode definitions from: pds_parser.py ---")
# print(f"--- Successfully imported PdsParser from: {PdsParser.module}.py ---") # Assumes PdsParser has a 'module' attribute
# print(f"--- Successfully imported PdsDiffer from: {PdsDiffer.module}.py ---") # Assumes PdsDiffer has a 'module' attribute
print("-" * 80)
# --- Benchmark File Paths ---
BENCHMARK_DIR = os.path.join(os.getcwd(), "benchmark_files") 
BENCHMARK_OLD = os.path.join(BENCHMARK_DIR, "benchmark_test_old.txt")
BENCHMARK_MOD = os.path.join(BENCHMARK_DIR, "benchmark_test_mod.txt")
BENCHMARK_NEW = os.path.join(BENCHMARK_DIR, "benchmark_test_new.txt")

# --- File I/O Helpers (UNCHANGED from your previous script) ---
def get_file_content(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8-sig') as f: return f.read()
    except UnicodeDecodeError:
        try:
            with open(filepath, 'r', encoding='utf-8') as f: return f.read()
        except Exception as e_inner: sys.stderr.write(f"ERROR reading {filepath} (fallback): {e_inner}\n"); return None
    except FileNotFoundError: sys.stderr.write(f"WARNING: File not found: {filepath}\n"); return None
    except Exception as e: sys.stderr.write(f"ERROR reading {filepath}: {e}\n"); return None

def write_to_file(filepath, content):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    try:
        with open(filepath, 'w', encoding='utf-8-sig') as f: f.write(content); return True
    except Exception as e: sys.stderr.write(f"ERROR writing to {filepath}: {e}\n"); return False

def normalize_for_comparison(text):
    if not text:
        return ""
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    # Collapse multiple blank lines to a single blank line
    text = re.sub(r'\n(\s*\n)+', '\n\n', text)
    # Optionally, strip trailing spaces from each line
    lines = [line.strip() for line in text.split('\n')]
    text = '\n'.join(lines)
    # Collapse multiple blank lines again to a single blank line
    return re.sub(r'\n(\s*\n)+', '\n', text).strip()

# --- Assertion Helpers (UNCHANGED from your previous script) ---
def assert_node_value(root_nodes_list, diff_path_segments, expected_value=None, expected_type=None, check_comment_substring=None, original_comment_substring=None):
    path_str = '->'.join(diff_path_segments)
    node = _find_node_by_diff_path_in_tree(root_nodes_list, diff_path_segments)
    success = True; messages = []

    if node is None:
        messages.append(f"FAIL: Node not found at path '{path_str}'.")
        success = False
    else:
        if expected_type is not None and not isinstance(node, expected_type):
             messages.append(f"FAIL: Node at '{path_str}' has incorrect type. Expected {expected_type.__name__}, got {type(node).__name__}.")
             success = False
        
        actual_value = None 
        
        if isinstance(node, (PdsKeyValuePair, PdsOperatorCondition)): actual_value = node.value
        elif isinstance(node, PdsList): actual_value = "<PdsList>" 
        elif isinstance(node, PdsBlock):
             if expected_value is not None and not isinstance(expected_value, (str, int, float, type(None))): 
                 messages.append(f"ASSERTION ERROR: Invalid expected_value '{expected_value}' type for PdsBlock key check: {type(expected_value)}. Must be str, number, or None for anonymous blocks.")
                 success = False
             else: 
                actual_value_for_block_key = node.key 
                if expected_value is not None and actual_value_for_block_key != expected_value:
                     messages.append(f"FAIL: Block key mismatch at '{path_str}'. Expected key '{expected_value}', got '{actual_value_for_block_key}'.")
                     success = False
                elif expected_value is None and actual_value_for_block_key is not None: 
                     messages.append(f"FAIL: Block key mismatch at '{path_str}'. Expected anonymous (None key), got key '{actual_value_for_block_key}'.")
                     success = False
                actual_value = f"<PdsBlock key='{node.key}'>" if node.key is not None else "<Anonymous PdsBlock>"
        elif isinstance(node, PdsComment): actual_value = node.comment_text
        elif isinstance(node, PdsBlankLine): actual_value = "<PdsBlankLine>" 

        if expected_value is not None:
             if isinstance(node, (PdsKeyValuePair, PdsOperatorCondition, PdsComment)): 
                 if actual_value != expected_value:
                     messages.append(f"FAIL: Value mismatch at '{path_str}'. Expected '{expected_value}', got '{actual_value}'.")
                     success = False
             elif isinstance(node, PdsList) and isinstance(expected_value, (list, tuple, set)): 
                 actual_list_comps = []
                 for v_item in node.values:
                     if isinstance(v_item, PdsNode): actual_list_comps.append(v_item.get_structural_components(shallow_block=False))
                     else: actual_list_comps.append(v_item)
                 
                 expected_list_comps_processed = []
                 for e_item in expected_value:
                     if isinstance(e_item, PdsNode): expected_list_comps_processed.append(e_item.get_structural_components(shallow_block=False))
                     else: expected_list_comps_processed.append(e_item)

                 if isinstance(expected_value, set): 
                     if set(actual_list_comps) != set(expected_list_comps_processed):
                         messages.append(f"FAIL: List content (set) mismatch at '{path_str}'.\n  Expected (any order): {set(expected_list_comps_processed)}\n  Got: {set(actual_list_comps)}")
                         success = False
                 else: 
                     if actual_list_comps != expected_list_comps_processed:
                          messages.append(f"FAIL: List content (ordered) mismatch at '{path_str}'.\n  Expected: {expected_list_comps_processed}\n  Got:      {actual_list_comps}")
                          success = False
        
        if check_comment_substring is not None:
            sim_comment_found = False
            node_line_comment = getattr(node, 'comment_text_on_line', None)
            if node_line_comment and check_comment_substring in node_line_comment:
                sim_comment_found = True
            
            if not sim_comment_found and isinstance(node, PdsBlock): 
                 if any(isinstance(child, PdsComment) and child.comment_text and check_comment_substring in child.comment_text for child in node.children):
                      sim_comment_found = True
            
            if not sim_comment_found:
                 child_comments_text = [c.comment_text for c in node.children if isinstance(c, PdsComment)] if isinstance(node, PdsBlock) else []
                 messages.append(f"FAIL: SimMerge comment substring '{check_comment_substring}' NOT found for '{path_str}'. Line comment: '{node_line_comment}'. Child comments: {child_comments_text}.")
                 success = False

        if original_comment_substring is not None:
            orig_comment_found = False
            node_line_comment = getattr(node, 'comment_text_on_line', None)
            if node_line_comment and original_comment_substring in node_line_comment:
                orig_comment_found = True
            
            if not orig_comment_found and isinstance(node, PdsComment) and node.comment_text and original_comment_substring in node.comment_text:
                orig_comment_found = True

            if not orig_comment_found and isinstance(node, PdsBlock): 
                 if any(isinstance(child, PdsComment) and child.comment_text and original_comment_substring in child.comment_text for child in node.children):
                      orig_comment_found = True
            
            if not orig_comment_found:
                 child_comments_text = [c.comment_text for c in node.children if isinstance(c, PdsComment)] if isinstance(node, PdsBlock) else []
                 main_comment_text = node.comment_text if isinstance(node, PdsComment) else "N/A"
                 messages.append(f"FAIL: Original comment substring '{original_comment_substring}' NOT found for '{path_str}'. Line comment: '{node_line_comment}'. Main comment: '{main_comment_text}'. Child comments: {child_comments_text}.")
                 success = False

    if success and node is not None: print(f"  PASS: Node at '{path_str}' matches expectations.")
    elif success and node is None and expected_value is None and expected_type is None: 
        print(f"  PASS: Node at '{path_str}' is correctly absent.")
    else: 
        if messages: 
             for msg in messages: print(f"  {msg}")
        elif node is not None and expected_value is None and expected_type is None: 
             print(f"  FAIL: Node at '{path_str}' was found ({node!r}), but expected to be absent.")
    return success

def assert_node_absent(root_nodes_list, diff_path_segments):
    path_str = '->'.join(diff_path_segments)
    node = _find_node_by_diff_path_in_tree(root_nodes_list, diff_path_segments)
    if node is None:
        print(f"  PASS: Node at '{path_str}' is correctly absent.")
        return True
    else:
        print(f"  FAIL: Node at '{path_str}' was found, but expected to be absent. Found node: {node!r}")
        return False

# --- Test Runner (UNCHANGED from your previous script) ---
def run_benchmark_test(test_name, old_path, mod_path, new_path, assertions_func):
    global g_subsumed_mod_block_paths_for_simulation 
    g_subsumed_mod_block_paths_for_simulation = set() 
    print(f"\n{'='*20} RUNNING BENCHMARK TEST: {test_name} {'='*20}")
    safe_test_name = "".join(c if c.isalnum() or c in (' ', '_', '-') else '_' for c in test_name)
    safe_test_name = safe_test_name.replace(" ", "_")
    test_output_dir = os.path.join(os.getcwd(), "test_output", "benchmark", safe_test_name)
    os.makedirs(test_output_dir, exist_ok=True)
    print(f"Output files for this test will be saved to: {test_output_dir}")
    old_content_raw = get_file_content(old_path); mod_content_raw = get_file_content(mod_path); new_content_raw = get_file_content(new_path)
    if old_content_raw is None and mod_content_raw is None and new_content_raw is None:
        print(f"SKIPPING TEST '{test_name}': All three input files are missing or could not be read."); return
    write_to_file(os.path.join(test_output_dir, os.path.basename(old_path).replace(".txt", "_OLD_RAW.txt")), old_content_raw or "")
    write_to_file(os.path.join(test_output_dir, os.path.basename(mod_path).replace(".txt", "_MOD_RAW.txt")), mod_content_raw or "")
    write_to_file(os.path.join(test_output_dir, os.path.basename(new_path).replace(".txt", "_NEW_RAW.txt")), new_content_raw or "")
    print(f"  Raw files saved.")
    parser = PdsParser() 
    print(f"  Parsing Old file: {os.path.basename(old_path)}"); old_nodes = parser.parse_file(old_path) if old_content_raw is not None else []
    print(f"  Parsing Mod file: {os.path.basename(mod_path)}"); mod_nodes = parser.parse_file(mod_path) if mod_content_raw is not None else []
    print(f"  Parsing New file: {os.path.basename(new_path)}"); new_nodes = parser.parse_file(new_path) if new_content_raw is not None else []
    if not (old_nodes or mod_nodes or new_nodes): print(f"SKIPPING TEST '{test_name}': No nodes parsed from any input file."); return
    reconstructed_old = PdsParser._nodes_to_string(old_nodes); reconstructed_mod = PdsParser._nodes_to_string(mod_nodes); reconstructed_new = PdsParser._nodes_to_string(new_nodes)
    write_to_file(os.path.join(test_output_dir, os.path.basename(old_path).replace(".txt", "_OLD_RECONSTRUCTED.txt")), reconstructed_old)
    write_to_file(os.path.join(test_output_dir, os.path.basename(mod_path).replace(".txt", "_MOD_RECONSTRUCTED.txt")), reconstructed_mod)
    write_to_file(os.path.join(test_output_dir, os.path.basename(new_path).replace(".txt", "_NEW_RECONSTRUCTED.txt")), reconstructed_new)
    print(f"  Reconstructed files saved.")
    normalized_old_raw = normalize_for_comparison(old_content_raw or ""); normalized_mod_raw = normalize_for_comparison(mod_content_raw or ""); normalized_new_raw = normalize_for_comparison(new_content_raw or "")
    if old_content_raw and normalize_for_comparison(reconstructed_old) != normalized_old_raw: print(f"\nWARNING: Normalized reconstruction mismatch for OLD file: {os.path.basename(old_path)}.")
    if mod_content_raw and normalize_for_comparison(reconstructed_mod) != normalized_mod_raw: print(f"\nWARNING: Normalized reconstruction mismatch for MOD file: {os.path.basename(mod_path)}.")
    if new_content_raw and normalize_for_comparison(reconstructed_new) != normalized_new_raw: print(f"\nWARNING: Normalized reconstruction mismatch for NEW file: {os.path.basename(new_path)}.")
    differ = PdsDiffer(); changes = differ.diff_nodes(old_nodes, mod_nodes, new_nodes)
    print(f"\n--- DETECTED CHANGES for '{test_name}' ({len(changes)} changes) ---")
    if not changes: print("    No significant changes detected by PdsDiffer.")
    max_changes_to_print = 50 
    for i, change in enumerate(changes):
        if i < max_changes_to_print: print(change)
        elif i == max_changes_to_print: print(f"    ... (omitting {len(changes) - max_changes_to_print} more changes)"); break
    
    changes_filepath = os.path.join(test_output_dir, "detected_changes_log.txt")
    with open(changes_filepath, 'w', encoding='utf-8') as f_changes:
        for change in changes:
            f_changes.write(repr(change) + "\n")
    print(f"  Full list of detected changes saved to: {os.path.basename(changes_filepath)}")

    print(f"\n--- Test Script: Requesting PdsDiffer to SIMULATE MERGE for '{test_name}' ---")
    simulated_merged_nodes_root_list = differ.simulate_three_way_merge(changes, new_nodes)
    simulated_merged_content = PdsParser._nodes_to_string(simulated_merged_nodes_root_list)
    merged_filename_base = os.path.basename(new_path).replace(".txt", "") if new_path else "unknown_new_file"
    sim_merged_filepath = os.path.join(test_output_dir, f"{merged_filename_base}_SIMULATED_MERGED.txt")
    write_to_file(sim_merged_filepath, simulated_merged_content)
    print(f"\n  Simulated merged content saved by Test Script to: {os.path.basename(sim_merged_filepath)}")
    print(f"\n--- Asserting Specific Merge Outcomes for '{test_name}' ---"); assertions_func(simulated_merged_nodes_root_list)
    print("--- Assertions Complete ---")
    print(f"\n--- DIFF: SIMULATED MERGED vs NORMALIZED NEW VANILLA RAW for '{test_name}' ---")
    diff_filename = os.path.join(test_output_dir, f"{merged_filename_base}_MERGED_VS_NEW_RAW.diff")
    normalized_simulated_merged = normalize_for_comparison(simulated_merged_content); diff_lines_exist = False
    if new_content_raw is not None: 
        with open(diff_filename, 'w', encoding='utf-8') as f_diff:
            diff_lines = list(difflib.unified_diff( normalized_new_raw.splitlines(keepends=True), normalized_simulated_merged.splitlines(keepends=True), fromfile='NORMALIZED_NEW_VANILLA_RAW', tofile='NORMALIZED_SIMULATED_MERGED', lineterm=''))
            if diff_lines: f_diff.writelines(diff_lines); print(f"  Diff (Normalized) saved to: {os.path.basename(diff_filename)}"); diff_lines_exist = True
            else: print("  NORMALIZED SIMULATED MERGED is identical to NORMALIZED NEW VANILLA RAW.")
    else: print(f"  Skipping diff generation because NEW vanilla raw content was not available for '{test_name}'.")
    if not diff_lines_exist and new_content_raw is not None: print("  CONFIRMED: NORMALIZED SIMULATED MERGED is identical to NORMALIZED NEW VANILLA RAW.")
    elif new_content_raw is not None: print(f"  ATTENTION: Diff found between NORMALIZED SIMULATED MERGED and NEW VANILLA RAW. Review '{os.path.basename(diff_filename)}'.")
    print("-" * 80)

# --- Define Benchmark Assertions Function (UNCHANGED from your previous script) ---
def benchmark_assertions(merged_nodes):
    print("\n--- Running Specific Benchmark Assertions ---")
    # Section 1
    assert_node_value(merged_nodes, ['primitive_string___0'], expected_value="mod_string_changed", expected_type=PdsKeyValuePair, check_comment_substring="SimMerge:CONFLICT_MODIFIED_MOD_CHOSEN")
    assert_node_value(merged_nodes, ['primitive_number___0'], expected_value=999.9, expected_type=PdsKeyValuePair, check_comment_substring="SimMerge:MOD_MODIFIED")
    assert_node_value(merged_nodes, ['primitive_bool___0'], expected_value=False, expected_type=PdsKeyValuePair, check_comment_substring="SimMerge:MOD_MODIFIED")
    assert_node_value(merged_nodes, ['primitive_float___0'], expected_value=2.0, expected_type=PdsKeyValuePair, check_comment_substring="SimMerge:VANILLA_MODIFIED")

    # Section 2
    assert_node_value(merged_nodes, ['delete_mod_modify_new___0'], expected_type=PdsBlock, check_comment_substring="SimMerge:CONFLICT_DELETION_MOD_DELETED_VANILLA_MODIFIED_VANILLA_MOD_KEPT", original_comment_substring="Comment New")
    assert_node_value(merged_nodes, ['delete_mod_modify_new___0', 'value___0'], expected_value=2)
    assert_node_value(merged_nodes, ['delete_mod_modify_new___0', 'keep_me___0'], expected_value=True)
    assert_node_value(merged_nodes, ['delete_mod_modify_new___0', 'vanilla_added_child___0'], expected_value=False, check_comment_substring="SimMerge:VANILLA_ADDED")
    
    mod_added_kvp_node = next((n for n in merged_nodes if isinstance(n, PdsKeyValuePair) and n.key == "add_mod_only"), None)
    if mod_added_kvp_node:
        assert_node_value(merged_nodes, [get_node_diff_key_for_find(mod_added_kvp_node)], expected_value="mod_added", check_comment_substring="SimMerge:MOD_ADDED", original_comment_substring="Comment Mod Added")
    else:
        print("  FAIL: MOD_ADDED node 'add_mod_only' not found at ROOT for assertion.")

    assert_node_absent(merged_nodes, ['delete_new_only___0']) 
    
    mod_modified_val_new_deleted_node = next((n for n in merged_nodes if isinstance(n, PdsKeyValuePair) and n.key == "modify_mod_delete_new"), None)
    if mod_modified_val_new_deleted_node:
         assert_node_value(merged_nodes, [get_node_diff_key_for_find(mod_modified_val_new_deleted_node)], expected_value="modded_value", check_comment_substring="SimMerge:CONFLICT_DELETION_VANILLA_DELETED_MOD_MODIFIED_MOD_MOD_APPLIED_AS_ADD", original_comment_substring="Comment Mod Modified")
    else:
        print("  FAIL: Node 'modify_mod_delete_new' (Mod modified, New deleted) not found at ROOT for assertion.")

    assert_node_value(merged_nodes, ['delete_mod_keep_new___0'], expected_value="original", check_comment_substring="SimMerge:MOD_DELETED") 
    
    assert_node_value(merged_nodes, ['keep_identical___0'], expected_value="unchanged", original_comment_substring="Comment Identical") 

    mod_comment_text = "This is a modded comment"
    comment_node_found = next((n for n in merged_nodes if isinstance(n, PdsComment) and n.comment_text == mod_comment_text), None)
    if comment_node_found:
        assert_node_value(merged_nodes, [get_node_diff_key_for_find(comment_node_found)], expected_value=mod_comment_text, check_comment_substring="SimMerge:CONFLICT_MODIFIED_MOD_CHOSEN")
    else:
        print(f"  FAIL: Expected comment node with text '{mod_comment_text}' not found.")

    mod_blank_replacement_text = "Mod replaced blank with comment" 
    blank_replacement_node = next((n for n in merged_nodes if isinstance(n, PdsComment) and n.comment_text == mod_blank_replacement_text), None)
    if blank_replacement_node: # Note: PdsComment stores text without leading '#'
        assert_node_value(merged_nodes, [get_node_diff_key_for_find(blank_replacement_node)], expected_value=mod_blank_replacement_text, check_comment_substring="SimMerge:CONFLICT_MODIFIED_MOD_CHOSEN")
    else:
        print(f"  FAIL: Expected blank line replacement comment '{mod_blank_replacement_text}' not found.")


    # Section 3: Lists
    list_path_simple = ['simple_list_conflict___0']
    assert_node_value(merged_nodes, list_path_simple, expected_type=PdsList, check_comment_substring="SimMerge:CONFLICT_MODIFIED_ITEMS_MERGED_UNION", original_comment_substring="Vanilla changed b->item_v, c->item_e, added item_added_vanilla") 
    expected_simple_list_items = {"item_a", "item_mod", "item_d", "item_added_mod", "item_v", "item_e", "item_added_vanilla"} 
    assert_node_value(merged_nodes, list_path_simple, expected_value=expected_simple_list_items, expected_type=PdsList)

    list_path_multi = ['multi_line_list___0']
    assert_node_value(merged_nodes, list_path_multi, expected_type=PdsList, check_comment_substring="SimMerge:CONFLICT_MODIFIED_ITEMS_MERGED_UNION")
    expected_multi_list_items = {"item_1", "item_2", "item_mod_added", "item_vanilla_added"}
    assert_node_value(merged_nodes, list_path_multi, expected_value=expected_multi_list_items, expected_type=PdsList)
    
    list_anon_path = ['list_with_anon_blocks___0']
    assert_node_value(merged_nodes, list_anon_path, expected_type=PdsList, check_comment_substring="SimMerge:CONFLICT_MODIFIED_ITEMS_MERGED_UNION")
    list_node_anon = _find_node_by_diff_path_in_tree(merged_nodes, list_anon_path)
    if list_node_anon:
        b1_mod_node = next((item for item in list_node_anon.values if isinstance(item, PdsBlock) and item.find_child_by_key("block_item_1") and item.find_child_by_key("block_item_1").value == "mod_value_changed"), None)
        if b1_mod_node: assert_node_value([b1_mod_node], [get_node_diff_key_for_find(b1_mod_node), 'block_item_1___0'], expected_value="mod_value_changed", check_comment_substring="SimMerge:CONFLICT_MODIFIED_MOD_CHOSEN") 
        else: print(f"  FAIL: Anon block with 'block_item_1 = mod_value_changed' not found in list_with_anon_blocks.")

        b2_van_node = next((item for item in list_node_anon.values if isinstance(item, PdsBlock) and item.find_child_by_key("block_item_2") and item.find_child_by_key("block_item_2").value == "vanilla_value_changed"), None)
        if b2_van_node: assert_node_value([b2_van_node], [get_node_diff_key_for_find(b2_van_node), 'block_item_2___0'], expected_value="vanilla_value_changed", check_comment_substring="SimMerge:VANILLA_MODIFIED") 
        else: print(f"  FAIL: Anon block with 'block_item_2 = vanilla_value_changed' not found in list_with_anon_blocks.")

        b3_mod_added_node = next((item for item in list_node_anon.values if isinstance(item, PdsBlock) and item.find_child_by_key("block_item_3_mod")), None)
        if b3_mod_added_node: assert_node_value([b3_mod_added_node], [get_node_diff_key_for_find(b3_mod_added_node), 'block_item_3_mod___0'], expected_value="mod_added", check_comment_substring="SimMerge:MOD_ADDED")
        else: print(f"  FAIL: Mod's added anon block ('block_item_3_mod') not found in list_with_anon_blocks.")

        b3_van_added_node = next((item for item in list_node_anon.values if isinstance(item, PdsBlock) and item.find_child_by_key("block_item_3_vanilla")), None)
        if b3_van_added_node: assert_node_value([b3_van_added_node], [get_node_diff_key_for_find(b3_van_added_node), 'block_item_3_vanilla___0'], expected_value="vanilla_added", check_comment_substring="SimMerge:VANILLA_ADDED")
        else: print(f"  FAIL: Vanilla's added anon block ('block_item_3_vanilla') not found in list_with_anon_blocks.")
    else: print(f"  FAIL: 'list_with_anon_blocks___0' node not found.")


    # Section 4: Blocks
    block_path = ['simple_block_conflict___0']
    assert_node_value(merged_nodes, block_path, expected_type=PdsBlock, check_comment_substring="SimMerge:CONFLICT_MODIFIED_CONTENTS_MERGED_BY_CHILDREN", original_comment_substring="Vanilla adds comment to block") 
    assert_node_value(merged_nodes, block_path + ['child_kvp___0'], expected_value="mod_value_changed", check_comment_substring="SimMerge:CONFLICT_MODIFIED_MOD_CHOSEN") 
    
    list_child_path = block_path + ['child_list___0'] 
    assert_node_value(merged_nodes, list_child_path, expected_type=PdsList, check_comment_substring="SimMerge:CONFLICT_MODIFIED_ITEMS_MERGED_UNION")
    expected_child_list_items = {"list_item_x", "list_item_y", "list_item_mod_y", "list_item_vanilla_z"}
    assert_node_value(merged_nodes, list_child_path, expected_value=expected_child_list_items, expected_type=PdsList)

    assert_node_value(merged_nodes, block_path + ['mod_added_child___0'], expected_value=True, check_comment_substring="SimMerge:MOD_ADDED") 

    parent_path = ['parent_block___0']
    assert_node_value(merged_nodes, parent_path, expected_type=PdsBlock, check_comment_substring="SimMerge:VANILLA_MODIFIED", original_comment_substring="Vanilla adds block comment")

    level1_mod_path = parent_path + ['child_block_level1_mod___0']
    assert_node_value(merged_nodes, level1_mod_path, expected_type=PdsBlock, check_comment_substring="SimMerge:CONFLICT_MODIFIED_CONTENTS_MERGED_BY_CHILDREN", original_comment_substring="Vanilla adds comment") 

    level2_path = level1_mod_path + ['child_block_level2___0']
    assert_node_value(merged_nodes, level2_path, expected_type=PdsBlock, check_comment_substring="SimMerge:CONFLICT_MODIFIED_CONTENTS_MERGED_BY_CHILDREN") 
    assert_node_value(merged_nodes, level2_path + ['final_value___0'], expected_value="modded", check_comment_substring="SimMerge:CONFLICT_MODIFIED_MOD_CHOSEN") 
    assert_node_value(merged_nodes, level2_path + ['mod_added_kvp___0'], expected_value=1, check_comment_substring="SimMerge:MOD_ADDED") 
    assert_node_value(merged_nodes, level2_path + ['vanilla_added_kvp___0'], expected_value=2, check_comment_substring="SimMerge:VANILLA_ADDED") 
    
    assert_node_value(merged_nodes, level1_mod_path + ['mod_added_at_level1___0'], expected_value="mod", check_comment_substring="SimMerge:MOD_ADDED") 

    level1_new_path = parent_path + ['child_block_level1_new___0'] 
    assert_node_value(merged_nodes, level1_new_path, expected_type=PdsBlock, check_comment_substring="SimMerge:MOD_DELETED") 
    assert_node_value(merged_nodes, level1_new_path + ['should_be_deleted_by_mod___0'], expected_value=True)

    parent_block_node = _find_node_by_diff_path_in_tree(merged_nodes, parent_path)
    vanilla_added_block_node = None
    if parent_block_node and isinstance(parent_block_node, PdsBlock):
        vanilla_added_block_node = parent_block_node.find_child_by_key("vanilla_added_block_level1")
    
    if vanilla_added_block_node:
         assert_node_value(parent_block_node.children, [get_node_diff_key_for_find(vanilla_added_block_node)], expected_value="vanilla_added_block_level1", check_comment_substring="SimMerge:VANILLA_ADDED")
    else: print(f"  FAIL: Vanilla added block 'vanilla_added_block_level1' not found in 'parent_block'.")

    mod_mod_del_new_block_node = next((n for n in merged_nodes if isinstance(n, PdsBlock) and n.key == "modify_mod_delete_new_block"), None)
    if mod_mod_del_new_block_node:
        base_path_mod_mod_del_new_block = [get_node_diff_key_for_find(mod_mod_del_new_block_node)]
        assert_node_value(merged_nodes, base_path_mod_mod_del_new_block, expected_type=PdsBlock, check_comment_substring="SimMerge:CONFLICT_DELETION_VANILLA_DELETED_MOD_MODIFIED_MOD_MOD_APPLIED_AS_ADD")
        assert_node_value(merged_nodes, base_path_mod_mod_del_new_block + ['initial_child___0'], expected_value=1)
        assert_node_value(merged_nodes, base_path_mod_mod_del_new_block + ['new_child_mod___0'], expected_value=True, check_comment_substring="SimMerge:MOD_ADDED") 
    else: print(f"  FAIL: Block 'modify_mod_delete_new_block' not found.")
    
    block_path_del_mod_mod_new = ['delete_mod_modify_new_block___0']
    assert_node_value(merged_nodes, block_path_del_mod_mod_new, expected_type=PdsBlock, check_comment_substring="SimMerge:CONFLICT_DELETION_MOD_DELETED_VANILLA_MODIFIED_VANILLA_MOD_KEPT", original_comment_substring="Comment DelModBlock New")
    assert_node_value(merged_nodes, block_path_del_mod_mod_new + ['initial_child___0'], expected_value=1) 
    assert_node_value(merged_nodes, block_path_del_mod_mod_new + ['new_child_van___0'], expected_value=True, check_comment_substring="SimMerge:VANILLA_ADDED") 

    # Section 5: Operator Conditions
    condition_path = ['condition_example___0']
    assert_node_value(merged_nodes, condition_path, expected_type=PdsBlock, check_comment_substring="SimMerge:CONFLICT_MODIFIED_CONTENTS_MERGED_BY_CHILDREN", original_comment_substring="Vanilla adds to block comment line")
    
    op_node_path = condition_path + ['base_value___0']
    assert_node_value(merged_nodes, op_node_path, expected_value=10, expected_type=PdsOperatorCondition, check_comment_substring="SimMerge:CONFLICT_MODIFIED_MOD_CHOSEN")
    op_node = _find_node_by_diff_path_in_tree(merged_nodes, op_node_path)
    if op_node and op_node.operator != ">=": print(f"  FAIL: condition_example->base_value operator. Expected '>=', Got '{op_node.operator}'.")
    
    assert_node_value(merged_nodes, condition_path + ['always_true___0'], expected_value=False, expected_type=PdsKeyValuePair, check_comment_substring="SimMerge:VANILLA_MODIFIED")
    
    # Section 6: Edge Cases
    kvp_to_block_path = ['kvp_to_block___0']
    assert_node_value(merged_nodes, kvp_to_block_path, expected_type=PdsKeyValuePair, check_comment_substring="SimMerge:CONFLICT_MODIFIED_MOD_CHOSEN")
    kvp_node_val_is_block = _find_node_by_diff_path_in_tree(merged_nodes, kvp_to_block_path)
    if kvp_node_val_is_block and isinstance(kvp_node_val_is_block.value, PdsBlock):
        print(f"  PASS: kvp_to_block value is correctly a PdsBlock.")
        if tuple(kvp_to_block_path) in g_subsumed_mod_block_paths_for_simulation:
             print(f"  PASS: Path {kvp_to_block_path} correctly in subsumed_paths for MOD_CHOSEN of block value.")
        else:
             print(f"  FAIL: Path {kvp_to_block_path} NOT in subsumed_paths despite MOD_CHOSEN of block value.")
        assert_node_value([kvp_node_val_is_block.value], ['child_in_block___0'], expected_value="mod_value")
        assert_node_value([kvp_node_val_is_block.value], ['another_child___0'], expected_value=True)
    else: print(f"  FAIL: kvp_to_block node not found or value is not PdsBlock. Value type: {type(getattr(kvp_node_val_is_block,'value',None)).__name__}")

    assert_node_absent(merged_nodes, ['block_key_change_old___0']) 
    
    mod_key_block_node = next((n for n in merged_nodes if isinstance(n, PdsBlock) and n.key == "block_key_change_mod"), None)
    if mod_key_block_node: assert_node_value(merged_nodes, [get_node_diff_key_for_find(mod_key_block_node)], expected_value="block_key_change_mod", check_comment_substring="SimMerge:MOD_ADDED")
    else: print(f"  FAIL: Mod added block 'block_key_change_mod' not found.")
    
    new_key_block_node = next((n for n in merged_nodes if isinstance(n, PdsBlock) and n.key == "block_key_change_new"), None)
    if new_key_block_node: assert_node_value(merged_nodes, [get_node_diff_key_for_find(new_key_block_node)], expected_value="block_key_change_new", check_comment_substring="SimMerge:VANILLA_ADDED")
    else: print(f"  FAIL: Vanilla added block 'block_key_change_new' not found.")
        
    assert_node_value(merged_nodes, ['empty_block___0'], expected_type=PdsBlock, check_comment_substring="SimMerge:MOD_MODIFIED", original_comment_substring="Mod adds comment to empty block")

    bwc_path = ['block_with_comments___0']
    assert_node_value(merged_nodes, bwc_path, expected_type=PdsBlock, check_comment_substring="SimMerge:CONFLICT_MODIFIED_CONTENTS_MERGED_BY_CHILDREN", original_comment_substring="Vanilla adds to block comment") 
    assert_node_value(merged_nodes, bwc_path + ['__COMMENT_5233920651969734260___0'], expected_value="Initial child comment Old 1 # Mod modifies comment 1", check_comment_substring="SimMerge:CONFLICT_MODIFIED_MOD_CHOSEN")
    assert_node_value(merged_nodes, bwc_path + ['__COMMENT_5233920651969734260___1'], expected_value="Initial child comment Old 2 # Vanilla modifies comment 2", check_comment_substring="SimMerge:VANILLA_MODIFIED")
    mod_added_child_comment_node = next((c for c in _find_node_by_diff_path_in_tree(merged_nodes, bwc_path).children if isinstance(c, PdsComment) and c.comment_text == "Mod adds child comment 2"), None)
    if mod_added_child_comment_node: assert_node_value([mod_added_child_comment_node], [get_node_diff_key_for_find(mod_added_child_comment_node)], check_comment_substring="SimMerge:MOD_ADDED")
    else: print("  FAIL: Mod added child comment 2 not found in block_with_comments")
    assert_node_value(merged_nodes, bwc_path + ['child___0'], expected_value=True) 
    assert_node_value(merged_nodes, bwc_path + ['mod_child___0'], expected_value=1, check_comment_substring="SimMerge:MOD_ADDED")


    # Section 7: Random List / Weighted Blocks
    rl_path = ['random_list_benchmark___0']
    assert_node_value(merged_nodes, rl_path, expected_type=PdsList, check_comment_substring="SimMerge:CONFLICT_MODIFIED_ITEMS_MERGED_UNION", original_comment_substring="Vanilla adds to block comment line")
    rl_node = _find_node_by_diff_path_in_tree(merged_nodes, rl_path)

    if rl_node and isinstance(rl_node, PdsList):
        def find_weighted_block(value_list, weight_key_str_or_int):
            weight_key_to_match = None
            try: weight_key_to_match = int(weight_key_str_or_int)
            except ValueError: weight_key_to_match = str(weight_key_str_or_int)

            for item in value_list:
                if isinstance(item, PdsBlock) and item.key is not None:
                    current_item_key_typed = None
                    try: current_item_key_typed = int(item.key)
                    except ValueError: current_item_key_typed = str(item.key)
                    
                    if current_item_key_typed == weight_key_to_match:
                        return item
            return None

        w10_node = find_weighted_block(rl_node.values, 10) # Key is int
        if w10_node:
            w10_path_prefix = [get_node_diff_key_for_find(w10_node)] # Path uses string key '10'
            assert_node_value([w10_node], w10_path_prefix, expected_value=10, check_comment_substring="SimMerge:CONFLICT_MODIFIED_CONTENTS_MERGED_BY_CHILDREN", original_comment_substring="Vanilla modifies comment") 
            assert_node_value([w10_node], w10_path_prefix + ['item_a___0'], expected_value="mod_value", check_comment_substring="SimMerge:CONFLICT_ADDITION_MOD_ALSO_ADDED") 
            assert_node_value([w10_node], w10_path_prefix + ['item_b___0'], expected_value=99, check_comment_substring="SimMerge:CONVERGED_MODIFICATION") 
            assert_node_value([w10_node], w10_path_prefix + ['mod_added_10___0'], expected_value=True, check_comment_substring="SimMerge:MOD_ADDED")
        else: print("  FAIL: Weighted block '10' not found in random_list_benchmark.")

        w20_node = find_weighted_block(rl_node.values, 20)
        if w20_node:
            w20_path_prefix = [get_node_diff_key_for_find(w20_node)]
            assert_node_value([w20_node], w20_path_prefix, expected_value=20, check_comment_substring="SimMerge:CONFLICT_DELETION_VANILLA_DELETED_MOD_MODIFIED_MOD_MOD_APPLIED_AS_ADD", original_comment_substring="Mod adds to block comment line")
            assert_node_value([w20_node], w20_path_prefix + ['item_c___0'], expected_value="modded")
            assert_node_value([w20_node], w20_path_prefix + ['mod_added_20___0'], expected_value=False, check_comment_substring="SimMerge:MOD_ADDED") 
        else: print("  FAIL: Weighted block '20' (Mod's version) not found in random_list_benchmark.")

        w30_node = find_weighted_block(rl_node.values, 30)
        if w30_node:
            w30_path_prefix = [get_node_diff_key_for_find(w30_node)]
            assert_node_value([w30_node], w30_path_prefix, expected_value=30, check_comment_substring="SimMerge:MOD_MODIFIED")
            assert_node_value([w30_node], w30_path_prefix + ['item_d___0'], expected_value=True, check_comment_substring="SimMerge:MOD_MODIFIED")
        else: print("  FAIL: Weighted block '30' not found in random_list_benchmark.")

        w40_node = find_weighted_block(rl_node.values, 40)
        if w40_node:
            w40_path_prefix = [get_node_diff_key_for_find(w40_node)]
            assert_node_value([w40_node], w40_path_prefix, expected_value=40, check_comment_substring="SimMerge:VANILLA_MODIFIED_CONTENTS_MERGED_BY_CHILDREN")
            assert_node_value([w40_node], w40_path_prefix + ['item_e___0'], expected_value=2.5, check_comment_substring="SimMerge:VANILLA_MODIFIED")
            assert_node_value([w40_node], w40_path_prefix + ['vanilla_added_40___0'], expected_value=False, check_comment_substring="SimMerge:VANILLA_ADDED")
        else: print("  FAIL: Weighted block '40' not found in random_list_benchmark.")

        w50_node = find_weighted_block(rl_node.values, 50)
        if w50_node:
            w50_path_prefix = [get_node_diff_key_for_find(w50_node)]
            assert_node_value([w50_node], w50_path_prefix, expected_value=50, check_comment_substring="SimMerge:CONFLICT_MODIFIED_CONTENTS_MERGED_BY_CHILDREN")
            assert_node_value([w50_node], w50_path_prefix + ['item_f___0'], expected_value="keep", check_comment_substring="SimMerge:CONVERGED_MODIFICATION")
            
            mod_comment_50_found = any(isinstance(c,PdsComment) and "Mod adds comment to 50 block" in c.comment_text for c in w50_node.children)
            van_comment_50_found = any(isinstance(c,PdsComment) and "Vanilla adds comment to 50 block" in c.comment_text for c in w50_node.children)
            if mod_comment_50_found: print("  PASS: Mod comment found in block 50 children.")
            else: print("  FAIL: Mod comment NOT found in block 50 children.")
            if van_comment_50_found: print("  PASS: Vanilla comment (from New) found in block 50 children.") 
            else: print("  FAIL: Vanilla comment NOT found in block 50 children.")
        else: print("  FAIL: Weighted block '50' not found in random_list_benchmark.")

        w60_node = find_weighted_block(rl_node.values, 60)
        if w60_node:
            w60_path_prefix = [get_node_diff_key_for_find(w60_node)]
            assert_node_value([w60_node], w60_path_prefix, expected_value=60, check_comment_substring="SimMerge:MOD_ADDED")
            assert_node_value([w60_node], w60_path_prefix + ['item_g___0'], expected_value="mod_added_60")
            nested_block_kvp_node = w60_node.find_child_by_key("nested_mod_60")
            if nested_block_kvp_node and isinstance(nested_block_kvp_node, PdsKeyValuePair) and isinstance(nested_block_kvp_node.value, PdsBlock):
                print(f"  PASS: KVP 'nested_mod_60' with block value found in block 60.")
                assert_node_value([nested_block_kvp_node.value], ['mod_val___0'], expected_value=1) 
            else: print(f"  FAIL: KVP 'nested_mod_60' with block value not found or incorrect structure in block 60.")
        else: print(f"  FAIL: Weighted block '60' (Mod added) not found.")

        w70_node = find_weighted_block(rl_node.values, 70)
        if w70_node:
            w70_path_prefix = [get_node_diff_key_for_find(w70_node)]
            assert_node_value([w70_node], w70_path_prefix, expected_value=70, check_comment_substring="SimMerge:VANILLA_ADDED")
            assert_node_value([w70_node], w70_path_prefix + ['item_c___0'], expected_value="original")
            assert_node_value([w70_node], w70_path_prefix + ['new_added_70___0'], expected_value=True)
        else: print(f"  FAIL: Weighted block '70' (Vanilla added) not found.")

        w80_node = find_weighted_block(rl_node.values, 80)
        if w80_node:
            w80_path_prefix = [get_node_diff_key_for_find(w80_node)]
            assert_node_value([w80_node], w80_path_prefix, expected_value=80, check_comment_substring="SimMerge:VANILLA_ADDED")
            assert_node_value([w80_node], w80_path_prefix + ['item_h___0'], expected_value=5)
        else: print(f"  FAIL: Weighted block '80' (Vanilla added) not found.")
    else: print("  FAIL: random_list_benchmark node not found or not a PdsList.")

    print("\n--- Specific Benchmark Assertions Complete ---")


# --- Run Tests ---
if __name__ == "__main__":
    # Check if benchmark files exist before running the test
    if not all(os.path.exists(p) for p in [BENCHMARK_OLD, BENCHMARK_MOD, BENCHMARK_NEW]):
        print("ERROR: Benchmark files not found. Please run the 'generate_benchmark_files.ipynb' script first.")
    else:
        print("Benchmark files found. Running tests...")
        run_benchmark_test("Comprehensive Benchmark", BENCHMARK_OLD, BENCHMARK_MOD, BENCHMARK_NEW, benchmark_assertions)
        print("\n--- All Tests Complete ---")