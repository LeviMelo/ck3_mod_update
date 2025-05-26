# -----------------------------------------------------------------------------
# Crusader Kings III Mod Updater Script (v13.1 - Enhanced File Filtering, Error Handling)
# -----------------------------------------------------------------------------
# Author: AI Assistant (with extensive user feedback & apologies)
# Date: 2025-05-26
#
# Purpose:
# Compares a CK3 mod against an old vanilla version (its base) and a new
# vanilla version (target for update). It now uses a robust tree-based parser
# and a 3-way diffing algorithm to identify granular changes and conflicts.
# It provides interactive prompts for user resolution of conflicts and
# application of mod-specific changes.
#
# Key Features:
# 1. Uses a custom parser (pds_parser.py) to represent CK3 script files as a tree.
# 2. Employs a 3-way diff (mod vs old_vanilla vs new_vanilla) to categorize changes.
# 3. **Improved file filtering**: Only processes files where YOUR MOD has actually changed them (M vs O).
# 4. Interactive CLI prompts for resolving conflicts and applying mod changes.
# 5. Adds in-line comments to merged/added nodes for traceability.
# 6. Creates a new output mod folder. Originals are NEVER modified.
# -----------------------------------------------------------------------------

import os
import re
import shutil
import filecmp # Used for efficient file-level comparison
from datetime import datetime, timezone

# Import our new core components
from pds_parser import PdsParser, PdsBlock, PdsKeyValuePair, PdsList, PdsComment, PdsBlankLine
from pds_differ import PdsDiffer, PdsChange

# --- CONFIGURATION ---
MOD_SOURCE_DIR = r"C:\Users\Galaxy\Documents\Paradox Interactive\Crusader Kings III\mod\custom_changes"
OLD_VANILLA_DIR_REFERENCE = r"C:\Users\Galaxy\LEVI\jupyter\ck3_mod_update\old_ver"
GAME_VANILLA_DIR_NEW = r"C:\Program Files (x86)\Steam\steamapps\common\Crusader Kings III\game"
OUTPUT_SUBFOLDER_NAME = "updated_mod_v13_1_tree_merge" # Updated version name
FOLDERS_TO_PROCESS = ["common", "events"] # Consider adding "localization", "gui", etc. if needed
# --- END CONFIGURATION ---

# --- Global Variables ---
SCRIPT_EXECUTION_DIR = os.getcwd()
MOD_OUTPUT_DIR = os.path.join(SCRIPT_EXECUTION_DIR, OUTPUT_SUBFOLDER_NAME)

# --- Interaction Control Flags ---
# These flags now control automatic decisions across files or within a single file
AUTO_PROCESS_ALL_FILES_SESSION = False # Auto-proceed with file processing summary
AUTO_APPLY_ALL_CHANGES_THIS_FILE = False # Auto-apply all changes within current file
AUTO_APPLY_ALL_CHANGES_SESSION = False # Auto-apply all changes for all files in session

# --- Helper Functions ---
def log_message(message, level="INFO", indent=0):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    indent_space = "    " * indent
    # Custom levels for better output formatting
    level_str = f"[{level.upper():<10}]" if level in ["INFO", "WARN", "ERROR", "FATAL"] \
                else f"[{level.upper():<10}]"
    print(f"{timestamp} {level_str} {indent_space}{message}")

def get_user_confirmation(prompt_message, prompt_level="param_apply"):
    global AUTO_PROCESS_ALL_FILES_SESSION, AUTO_APPLY_ALL_CHANGES_THIS_FILE, AUTO_APPLY_ALL_CHANGES_SESSION

    # Auto-responses based on flags
    if prompt_level == "file_process_summary" and AUTO_PROCESS_ALL_FILES_SESSION:
        return "yes"
    if prompt_level in ["block_replace", "entry_add", "param_add_specific", "conflict_resolution", "delete_confirm"]:
        if AUTO_APPLY_ALL_CHANGES_THIS_FILE:
            return "yes"
        if AUTO_APPLY_ALL_CHANGES_SESSION:
            return "yes"

    base_options_text = "(y[es]/n[o]"
    if prompt_level == "file_process_summary":
        options_text = f"{base_options_text}/S[kip ALL file summaries & auto-process files])"
    elif prompt_level in ["block_replace", "entry_add", "param_add_specific", "conflict_resolution", "delete_confirm"]:
        options_text = f"{base_options_text}/a[pply ALL for current file]/s[kip ALL change confirmations for session])"
    else:
        options_text = f"{base_options_text})"

    while True:
        full_prompt = f"[PROMPT    ] {prompt_message} {options_text}: "
        response = input(full_prompt).strip().lower()
        if response in ["yes", "y"]: return "yes"
        if response in ["no", "n"]: return "no"
        if prompt_level == "file_process_summary":
            if response in ["s", "skipallfiles"]:
                AUTO_PROCESS_ALL_FILES_SESSION = True
                return "yes"
        elif prompt_level in ["block_replace", "entry_add", "param_add_specific", "conflict_resolution", "delete_confirm"]:
            if response in ["a", "applyall"]:
                AUTO_APPLY_ALL_CHANGES_THIS_FILE = True
                return "yes"
            if response in ["s", "skipall", "skipallconfirmations"]:
                AUTO_APPLY_ALL_CHANGES_SESSION = True
                return "yes"
        log_message("Invalid input. Please enter 'y', 'n', 'a', or 's'.", "ERROR", 1)

# PdsDiffer instance is passed to these functions
def find_node_by_path(root_nodes, key_path, differ_util):
    """
    Traverses the tree to find a node given its full key_path.
    root_nodes: list of PdsNode objects (top-level nodes in a file)
    key_path: list of strings, e.g., ['witch.1001', 'trigger', 'is_witch_trigger']
    differ_util: An instance of PdsDiffer to get node identifiers.
    """
    if not key_path:
        return None

    current_nodes = root_nodes
    target_node = None

    for i, segment in enumerate(key_path):
        found_in_current_level = False
        for node in current_nodes:
            if differ_util._get_node_identifier(node) == segment:
                if i == len(key_path) - 1: # This is the final node
                    target_node = node
                    found_in_current_level = True
                    break
                elif isinstance(node, PdsBlock): # Not final, but can recurse into its children
                    current_nodes = node.children
                    found_in_current_level = True
                    break
                else: # Segment found, but it's not a block and not the final node
                    return None # Path broken
        if not found_in_current_level:
            return None # Segment not found at this level
    return target_node

def get_parent_node_by_path(root_nodes, key_path, differ_util):
    """
    Traverses the tree to find the parent node given a child's full key_path.
    Returns (parent_node, child_identifier_in_parent).
    differ_util: An instance of PdsDiffer to get node identifiers.
    """
    if not key_path or len(key_path) < 1:
        return None, None
    
    if len(key_path) == 1: # Top-level node, parent is effectively the file root list
        return root_nodes, key_path[0] # Return list itself and identifier

    parent_path = key_path[:-1]
    child_identifier = key_path[-1]

    parent_block = find_node_by_path(root_nodes, parent_path, differ_util)
    if isinstance(parent_block, PdsBlock): # Parent must be a block to manipulate children
        return parent_block, child_identifier
    return None, None # Parent not found or not a block

# --- Core Merge Function ---
def process_single_file_merge(mod_rel_path, differ_instance):
    """
    Orchestrates the 3-way diff, interactive conflict resolution,
    and modification of the New Vanilla tree for a single file.
    """
    log_message(f"Processing file: '{mod_rel_path}'", "HEADER")
    global AUTO_APPLY_ALL_CHANGES_THIS_FILE # Reset for each file
    AUTO_APPLY_ALL_CHANGES_THIS_FILE = False

    mod_abs_path = os.path.join(MOD_SOURCE_DIR, mod_rel_path)
    old_vanilla_abs_path = os.path.join(OLD_VANILLA_DIR_REFERENCE, mod_rel_path)
    new_vanilla_abs_path = os.path.join(GAME_VANILLA_DIR_NEW, mod_rel_path)
    output_abs_path = os.path.join(MOD_OUTPUT_DIR, mod_rel_path)

    parser = PdsParser() # PdsParser can be instantiated per file as it manages internal state

    # Load and parse all three versions
    log_message("Loading file versions...", "INFO", 1)
    old_nodes = parser.parse_file(old_vanilla_abs_path)
    mod_nodes = parser.parse_file(mod_abs_path)
    new_nodes = parser.parse_file(new_vanilla_abs_path)

    if not new_nodes:
        log_message(f"New Vanilla file '{mod_rel_path}' not found or empty. Cannot merge.", "WARN", 1)
        # If NV doesn't exist, and Mod doesn't exist relative to Old, skip.
        # If Mod exists and NV doesn't, this means Mod added a file that NV deleted.
        if mod_nodes: # This is a file that existed in MOD_SOURCE but not in NEW_VANILLA
            prompt = f"Mod file '{mod_rel_path}' exists, but New Vanilla deleted it. Copy your mod's file as is?"
            if get_user_confirmation(prompt, prompt_level="file_process_summary") == "yes":
                os.makedirs(os.path.dirname(output_abs_path), exist_ok=True)
                shutil.copy2(mod_abs_path, output_abs_path)
                log_message(f"Copied '{mod_rel_path}' from mod source.", "SUCCESS", 2)
            else:
                log_message(f"Skipped copying mod file '{mod_rel_path}'.", "INFO", 2)
        return False # Indicate file was not merged into NV base

    # Create a deep copy of the new_nodes to apply changes to
    modified_output_nodes = [node.copy() for node in new_nodes]
    file_was_modified_by_script = False

    # Get changes from the differ
    changes = differ_instance.diff_nodes(old_nodes, mod_nodes, new_nodes)

    if not changes:
        log_message(f"No detected changes for '{mod_rel_path}'. Copying New Vanilla as is.", "INFO", 1)
        os.makedirs(os.path.dirname(output_abs_path), exist_ok=True)
        shutil.copy2(new_vanilla_abs_path, output_abs_path)
        return True # Indicate successful processing, even if just copy

    log_message(f"'{mod_rel_path}' has {len(changes)} detected changes:", "INFO", 1)
    for i, change in enumerate(changes):
        log_message(f"  Change {i+1}: {change.type:<25} at {'.'.join(change.key_path)}", "DETAIL", 2)
    
    prompt_file_msg = f"Proceed with processing '{mod_rel_path}' with {len(changes)} detected changes?"
    if get_user_confirmation(prompt_file_msg, prompt_level="file_process_summary") == "no":
        log_message(f"Skipping file '{mod_rel_path}' by user choice. No output generated.", "INFO", 1)
        return False

    # --- Apply Changes Interactively ---
    log_message("Applying changes interactively...", "PHASE", 1)
    
    # Iterate through changes and apply/resolve
    for i, change in enumerate(changes):
        log_message(f"\n--- Change {i+1}/{len(changes)}: {change.type} at {'.'.join(change.key_path)} ---", "INFO", 2)
        log_message(f"  Parent context: {'.'.join(change.context_parent_path) if change.context_parent_path else 'ROOT'}", "DETAIL", 3)

        # --- Display Nodes for Context ---
        log_message("  OLD VANILLA:", "DETAIL", 3)
        if change.old_node: 
            # Fix: Use PdsParser._nodes_to_string() and capture the actual text
            # Ensure it only prints the node itself and its immediate content
            print("    " + PdsParser._nodes_to_string([change.old_node]).strip())
        else: 
            print("    ABSENT")
        
        log_message("  YOUR MOD:", "DETAIL", 3)
        if change.mod_node: 
            print("    " + PdsParser._nodes_to_string([change.mod_node]).strip())
        else: 
            print("    ABSENT")

        log_message("  NEW VANILLA:", "DETAIL", 3)
        if change.new_node: 
            print("    " + PdsParser._nodes_to_string([change.new_node]).strip())
        else: 
            print("    ABSENT")

        # --- Get Parent Node for Manipulation ---
        # Pass differ_instance to helper functions
        parent_for_manipulation, child_identifier_in_parent = get_parent_node_by_path(modified_output_nodes, change.key_path, differ_instance)
        
        if parent_for_manipulation is None:
            log_message(f"WARNING: Could not find parent for '{'.'.join(change.key_path)}' in output tree. Skipping this change.", "ERROR", 3)
            continue
        
        # --- Decision Logic ---
        # All changes made to modified_output_nodes should involve .copy() of chosen node
        # Add comment for traceability
        timestamp_comment = datetime.now(timezone.utc).strftime("ScriptMerged:%Y%m%d%H%M%SZ")

        if change.type == 'MOD_ADDED':
            prompt = f"Mod added '{change.mod_node.key}'. Append to New Vanilla?"
            if get_user_confirmation(prompt, prompt_level="entry_add") == "yes":
                new_node_to_add = change.mod_node.copy()
                if isinstance(new_node_to_add, PdsKeyValuePair):
                    new_node_to_add.comment_text_on_line = (f"{new_node_to_add.comment_text_on_line} {timestamp_comment} MOD_ADDED" if new_node_to_add.comment_text_on_line else timestamp_comment + " MOD_ADDED")
                elif isinstance(new_node_to_add, PdsBlock):
                     new_node_to_add.comment_text_on_line = (f"{new_node_to_add.comment_text_on_line} {timestamp_comment} MOD_ADDED" if new_node_to_add.comment_text_on_line else timestamp_comment + " MOD_ADDED")
                
                if isinstance(parent_for_manipulation, list): # Root level addition
                    parent_for_manipulation.append(new_node_to_add)
                else: # Nested addition
                    parent_for_manipulation.add_child_at_appropriate_location(new_node_to_add)
                log_message(f"Added mod's '{change.mod_node.key}'.", "SUCCESS", 4)
                file_was_modified_by_script = True
            else:
                log_message(f"Skipped adding mod's '{change.mod_node.key}'.", "INFO", 4)

        elif change.type == 'VANILLA_ADDED':
            log_message(f"Vanilla added '{change.new_node.key}'. Automatically applying.", "INFO", 3)
            # It's already in modified_output_nodes (which is a copy of new_nodes), so no action needed.
            # However, if the node was a comment/blank and we were tracking its path via hash/line number
            # we should ensure it's there. For now, assume it's correctly in the base.
            pass # No direct action needed, it's already in our base output tree.

        elif change.type == 'MOD_MODIFIED':
            prompt = f"Mod modified '{change.mod_node.key}'. Replace New Vanilla's version with Mod's?"
            if get_user_confirmation(prompt, prompt_level="block_replace") == "yes":
                chosen_node = change.mod_node.copy()
                if isinstance(chosen_node, PdsKeyValuePair):
                    chosen_node.comment_text_on_line = (f"{chosen_node.comment_text_on_line} {timestamp_comment} MOD_MODIFIED" if chosen_node.comment_text_on_line else timestamp_comment + " MOD_MODIFIED")
                elif isinstance(chosen_node, PdsBlock):
                     chosen_node.comment_text_on_line = (f"{chosen_node.comment_text_on_line} {timestamp_comment} MOD_MODIFIED" if chosen_node.comment_text_on_line else timestamp_comment + " MOD_MODIFIED")

                if isinstance(parent_for_manipulation, list): # Root level modification
                    for idx, node in enumerate(parent_for_manipulation):
                        if differ_instance._get_node_identifier(node) == child_identifier_in_parent:
                            parent_for_manipulation[idx] = chosen_node
                            break
                else: # Nested modification
                    parent_for_manipulation.replace_child(child_identifier_in_parent, chosen_node)
                log_message(f"Applied mod's modified '{change.mod_node.key}'.", "SUCCESS", 4)
                file_was_modified_by_script = True
            else:
                log_message(f"Kept New Vanilla's version of '{change.new_node.key}'.", "INFO", 4)

        elif change.type == 'VANILLA_MODIFIED':
            log_message(f"Vanilla modified '{change.new_node.key}'. Automatically keeping New Vanilla's version.", "INFO", 3)
            pass # No action needed, as modified_output_nodes started as a copy of new_nodes

        elif change.type == 'CONFLICT_MODIFIED':
            log_message(f"Conflict: Both Mod and Vanilla modified '{change.key_path[-1]}'.", "WARN", 3)
            log_message("Options:", "INFO", 4)
            log_message("  1. Keep YOUR MOD's version", "INFO", 4)
            log_message("  2. Keep NEW VANILLA's version", "INFO", 4)
            log_message("  3. Skip (manual merge later)", "INFO", 4)
            choice = input("[PROMPT    ] Enter your choice (1/2/3): ").strip()

            chosen_node = None
            if choice == '1':
                chosen_node = change.mod_node.copy()
                log_message("User chose: Keep YOUR MOD's version.", "INFO", 4)
            elif choice == '2':
                chosen_node = change.new_node.copy()
                log_message("User chose: Keep NEW VANILLA's version.", "INFO", 4)
            else:
                log_message("User chose: Skip this conflict. Manual merge needed for this entry.", "WARN", 4)
                continue

            if chosen_node:
                if isinstance(chosen_node, PdsKeyValuePair):
                    chosen_node.comment_text_on_line = (f"{chosen_node.comment_text_on_line} {timestamp_comment} CONFLICT_MODIFIED" if chosen_node.comment_text_on_line else timestamp_comment + " CONFLICT_MODIFIED")
                elif isinstance(chosen_node, PdsBlock):
                     chosen_node.comment_text_on_line = (f"{chosen_node.comment_text_on_line} {timestamp_comment} CONFLICT_MODIFIED" if chosen_node.comment_text_on_line else timestamp_comment + " CONFLICT_MODIFIED")

                if isinstance(parent_for_manipulation, list): # Root level
                    for idx, node in enumerate(parent_for_manipulation):
                        if differ_instance._get_node_identifier(node) == child_identifier_in_parent:
                            parent_for_manipulation[idx] = chosen_node
                            break
                else: # Nested
                    parent_for_manipulation.replace_child(child_identifier_in_parent, chosen_node)
                log_message(f"Applied chosen version for '{change.key_path[-1]}'.", "SUCCESS", 4)
                file_was_modified_by_script = True
        
        elif change.type == 'CONFLICT_ADDITION':
            log_message(f"Conflict: Both Mod and Vanilla added an item with the same identifier '{change.key_path[-1]}', but with different content.", "WARN", 3)
            log_message("Options:", "INFO", 4)
            log_message("  1. Keep YOUR MOD's version (will overwrite NV's added item)", "INFO", 4)
            log_message("  2. Keep NEW VANILLA's version", "INFO", 4)
            log_message("  3. Skip (manual merge later or keep both if IDs are actually unique)", "INFO", 4)
            choice = input("[PROMPT    ] Enter your choice (1/2/3): ").strip()

            chosen_node = None
            if choice == '1':
                chosen_node = change.mod_node.copy()
                log_message("User chose: Keep YOUR MOD's added version.", "INFO", 4)
            elif choice == '2':
                chosen_node = change.new_node.copy()
                log_message("User chose: Keep NEW VANILLA's added version.", "INFO", 4)
            else:
                log_message("User chose: Skip this conflict. Manual merge needed.", "WARN", 4)
                continue

            if chosen_node:
                if isinstance(chosen_node, PdsKeyValuePair):
                    chosen_node.comment_text_on_line = (f"{chosen_node.comment_text_on_line} {timestamp_comment} CONFLICT_ADDITION" if chosen_node.comment_text_on_line else timestamp_comment + " CONFLICT_ADDITION")
                elif isinstance(chosen_node, PdsBlock):
                     chosen_node.comment_text_on_line = (f"{chosen_node.comment_text_on_line} {timestamp_comment} CONFLICT_ADDITION" if chosen_node.comment_text_on_line else timestamp_comment + " CONFLICT_ADDITION")

                # Remove NV's version first if it exists, then add the chosen_node
                # This handles the case where NV already has the node, but we're replacing it with Mod's version.
                # If NV's node wasn't added yet (because it was the 'other side' of the add conflict), this removal is a no-op.
                if isinstance(parent_for_manipulation, list): # Root level
                    parent_for_manipulation[:] = [
                        n for n in parent_for_manipulation 
                        if differ_instance._get_node_identifier(n) != child_identifier_in_parent
                    ]
                    parent_for_manipulation.append(chosen_node) # Add chosen node to root
                else: # Nested
                    # Try to replace if it exists, otherwise add. This assumes it's a replacement for an existing conflict.
                    if not parent_for_manipulation.replace_child(child_identifier_in_parent, chosen_node):
                        parent_for_manipulation.add_child_at_appropriate_location(chosen_node)

                log_message(f"Applied chosen added version for '{change.key_path[-1]}'.", "SUCCESS", 4)
                file_was_modified_by_script = True

        elif change.type == 'CONFLICT_DELETION':
            log_message(f"Conflict: Item '{change.key_path[-1]}' was deleted by one side and modified by the other.", "WARN", 3)
            log_message("Options:", "INFO", 4)
            log_message("  1. Keep Mod's deletion (remove the item)", "INFO", 4)
            log_message("  2. Keep New Vanilla's version (restore/keep the item as NV has it)", "INFO", 4)
            log_message("  3. Skip (manual merge later)", "INFO", 4)
            choice = input("[PROMPT    ] Enter your choice (1/2/3): ").strip()

            if choice == '1': # User chose to delete it (Mod's deletion)
                if isinstance(parent_for_manipulation, list):
                    parent_for_manipulation[:] = [
                        n for n in parent_for_manipulation 
                        if differ_instance._get_node_identifier(n) != child_identifier_in_parent
                    ]
                else:
                    parent_for_manipulation.remove_child(child_identifier_in_parent)
                log_message(f"Applied deletion for '{change.key_path[-1]}'.", "SUCCESS", 4)
                file_was_modified_by_script = True
            elif choice == '2': # User chose to keep NV's version
                chosen_node = change.new_node.copy()
                if isinstance(chosen_node, PdsKeyValuePair):
                    chosen_node.comment_text_on_line = (f"{chosen_node.comment_text_on_line} {timestamp_comment} CONFLICT_DELETION_KEPT_NV" if chosen_node.comment_text_on_line else timestamp_comment + " CONFLICT_DELETION_KEPT_NV")
                elif isinstance(chosen_node, PdsBlock):
                     chosen_node.comment_text_on_line = (f"{chosen_node.comment_text_on_line} {timestamp_comment} CONFLICT_DELETION_KEPT_NV" if chosen_node.comment_text_on_line else timestamp_comment + " CONFLICT_DELETION_KEPT_NV")

                if isinstance(parent_for_manipulation, list):
                    for idx, node in enumerate(parent_for_manipulation):
                        if differ_instance._get_node_identifier(node) == child_identifier_in_parent:
                            parent_for_manipulation[idx] = chosen_node
                            break
                else:
                    parent_for_manipulation.replace_child(child_identifier_in_parent, chosen_node)
                log_message(f"Applied New Vanilla's version for '{change.key_path[-1]}'.", "SUCCESS", 4)
                file_was_modified_by_script = True
            else:
                log_message("User chose: Skip this conflict. Manual merge needed.", "WARN", 4)
                continue

        elif change.type == 'MOD_DELETED':
            prompt = f"Mod deleted '{change.old_node.key}'. Remove it from New Vanilla?"
            if get_user_confirmation(prompt, prompt_level="delete_confirm") == "yes":
                if isinstance(parent_for_manipulation, list):
                    parent_for_manipulation[:] = [
                        n for n in parent_for_manipulation 
                        if differ_instance._get_node_identifier(n) != child_identifier_in_parent
                    ]
                else:
                    parent_for_manipulation.remove_child(child_identifier_in_parent)
                log_message(f"Removed '{change.old_node.key}' per mod's deletion.", "SUCCESS", 4)
                file_was_modified_by_script = True
            else:
                log_message(f"Kept '{change.old_node.key}' in New Vanilla.", "INFO", 4)
        
        elif change.type == 'VANILLA_DELETED':
            log_message(f"Vanilla deleted '{change.old_node.key}'. Automatically removing it from output.", "INFO", 3)
            # It should already be gone if output started as new_nodes.
            # But if there's a reference in the output tree from a previous merge, ensure it's removed.
            # Only remove if it *still exists* in the modified_output_nodes (e.g., if mod also had it and was kept)
            if find_node_by_path(modified_output_nodes, change.key_path, differ_instance) is not None:
                if isinstance(parent_for_manipulation, list):
                    parent_for_manipulation[:] = [
                        n for n in parent_for_manipulation 
                        if differ_instance._get_node_identifier(n) != child_identifier_in_parent
                    ]
                else:
                    parent_for_manipulation.remove_child(child_identifier_in_parent)
                log_message(f"Confirmed removal of vanilla-deleted '{change.old_node.key}'.", "SUCCESS", 4)
                file_was_modified_by_script = True
            else:
                log_message(f"Node '{change.key_path[-1]}' already absent in output. No action needed.", "INFO", 4)
        
        # Converged changes (MOD_ADDED_CONVERGED, MOD_DELETED_VANILLA_ALSO_DELETED, CONVERGED_MODIFICATION)
        # These typically require no user action as both sides ended up with the same state.
        elif change.type in ['MOD_ADDED_CONVERGED', 'MOD_DELETED_VANILLA_ALSO_DELETED', 'CONVERGED_MODIFICATION']:
            log_message(f"Converged change on '{change.key_path[-1]}'. Automatically applied (both sides agree).", "INFO", 3)
            # For deletions, we must ensure it's removed from the output tree if it happens to be there
            if change.type == 'MOD_DELETED_VANILLA_ALSO_DELETED':
                if find_node_by_path(modified_output_nodes, change.key_path, differ_instance) is not None:
                    if isinstance(parent_for_manipulation, list):
                        parent_for_manipulation[:] = [
                            n for n in parent_for_manipulation 
                            if differ_instance._get_node_identifier(n) != child_identifier_in_parent
                        ]
                    else:
                        parent_for_manipulation.remove_child(child_identifier_in_parent)
                    log_message(f"Confirmed converged deletion of '{change.key_path[-1]}'.", "SUCCESS", 4)
                    file_was_modified_by_script = True
        else:
            log_message(f"Unhandled change type (no action taken): {change.type} for '{change.key_path[-1]}'. Manual review needed.", "WARN", 3)

    # Write the modified output tree to the file
    if file_was_modified_by_script:
        os.makedirs(os.path.dirname(output_abs_path), exist_ok=True)
        final_output_content = PdsParser._nodes_to_string(modified_output_nodes)
        with open(output_abs_path, 'w', encoding='utf-8-sig') as f:
            f.write(final_output_content)
        log_message(f"Successfully merged changes and wrote '{mod_rel_path}' to output.", "SUCCESS", 1)
        return True # Indicate successful merge
    else:
        log_message(f"No changes applied to '{mod_rel_path}' by script based on detected diffs. Copying original New Vanilla.", "INFO", 1)
        os.makedirs(os.path.dirname(output_abs_path), exist_ok=True)
        shutil.copy2(new_vanilla_abs_path, output_abs_path)
        return False # Indicate no significant change was applied by the merge process


# --- Main Script Execution ---
def main():
    script_version = "v13.1 - Enhanced File Filtering, Error Handling"
    log_message(f"Script started. CK3 Mod Updater ({script_version}).", "HEAD")
    global AUTO_PROCESS_ALL_FILES_SESSION, AUTO_APPLY_ALL_CHANGES_THIS_FILE, AUTO_APPLY_ALL_CHANGES_SESSION
    AUTO_PROCESS_ALL_FILES_SESSION, AUTO_APPLY_ALL_CHANGES_THIS_FILE, AUTO_APPLY_ALL_CHANGES_SESSION = False, False, False
    
    paths_ok = True
    for p, n in [("MOD_SOURCE_DIR",MOD_SOURCE_DIR), ("OLD_VANILLA_DIR_REFERENCE",OLD_VANILLA_DIR_REFERENCE), ("GAME_VANILLA_DIR_NEW",GAME_VANILLA_DIR_NEW)]:
        if not os.path.isdir(n): log_message(f"{p} not found: '{n}'. Exiting.", "FATAL"); paths_ok=False
    if not paths_ok: return

    log_message(f"Output: '{MOD_OUTPUT_DIR}'", "INFO")
    log_message("Output dir will be DELETED/RECREATED.", "WARN")
    if get_user_confirmation(f"Proceed? (Deletes '{OUTPUT_SUBFOLDER_NAME}')", prompt_level="initial_script_start") == "no":
        log_message("Cancelled.", "INFO"); return
    
    if os.path.exists(MOD_OUTPUT_DIR):
        log_message(f"Removing '{MOD_OUTPUT_DIR}'...", "DETAIL")
        try: shutil.rmtree(MOD_OUTPUT_DIR)
        except Exception as e: log_message(f"Could not remove output dir: {e}. Exiting.", "FATAL"); return
    try: os.makedirs(MOD_OUTPUT_DIR, exist_ok=True)
    except Exception as e: log_message(f"Could not create output dir: {e}. Exiting.", "FATAL"); return
    log_message(f"Output dir ready: '{MOD_OUTPUT_DIR}'", "SUCCESS")

    processed_files_count = 0
    skipped_files_count = 0
    total_files_with_mod_changes = 0 # Files where mod had *some* change (added, modified, deleted)

    # Instantiate PdsDiffer once
    differ = PdsDiffer()

    # Walk through mod source directory AND new vanilla directory to find relevant files
    for folder_name in FOLDERS_TO_PROCESS:
        mod_folder_path = os.path.join(MOD_SOURCE_DIR, folder_name)
        new_vanilla_folder_path = os.path.join(GAME_VANILLA_DIR_NEW, folder_name)
        old_vanilla_folder_path = os.path.join(OLD_VANILLA_DIR_REFERENCE, folder_name)

        if not os.path.isdir(mod_folder_path) and not os.path.isdir(new_vanilla_folder_path):
            log_message(f"Skipping folder '{folder_name}': neither mod nor new vanilla path exists.", "WARN", 1)
            continue
        
        all_relevant_files_in_folder = set()

        # Add all .txt files from New Vanilla
        if os.path.isdir(new_vanilla_folder_path):
            for root, _, files in os.walk(new_vanilla_folder_path):
                for filename in files:
                    if filename.lower().endswith(".txt"):
                        rel_path = os.path.relpath(os.path.join(root, filename), GAME_VANILLA_DIR_NEW)
                        all_relevant_files_in_folder.add(rel_path)

        # Add all .txt files from Mod Source
        if os.path.isdir(mod_folder_path):
            for root, _, files in os.walk(mod_folder_path):
                for filename in files:
                    if filename.lower().endswith(".txt"):
                        rel_path = os.path.relpath(os.path.join(root, filename), MOD_SOURCE_DIR)
                        all_relevant_files_in_folder.add(rel_path)
        
        for mod_rel_path in sorted(list(all_relevant_files_in_folder)):
            mod_abs_path = os.path.join(MOD_SOURCE_DIR, mod_rel_path)
            old_vanilla_abs_path = os.path.join(OLD_VANILLA_DIR_REFERENCE, mod_rel_path)
            new_vanilla_abs_path = os.path.join(GAME_VANILLA_DIR_NEW, mod_rel_path)
            output_abs_path = os.path.join(MOD_OUTPUT_DIR, mod_rel_path)

            # --- Primary File Filtering Logic ---
            # Scenario 1: File is NEW IN YOUR MOD (exists in Mod, not in New Vanilla)
            if os.path.exists(mod_abs_path) and not os.path.exists(new_vanilla_abs_path):
                log_message(f"File '{mod_rel_path}' is NEW IN YOUR MOD (not in New Vanilla).", "INFO")
                prompt = f"Mod file '{mod_rel_path}' is new. Copy to updated mod as is?"
                if get_user_confirmation(prompt, prompt_level="file_process_summary") == "yes":
                    os.makedirs(os.path.dirname(output_abs_path), exist_ok=True)
                    shutil.copy2(mod_abs_path, output_abs_path)
                    log_message(f"Copied new mod file '{mod_rel_path}'.", "SUCCESS", 1)
                    processed_files_count += 1
                else:
                    log_message(f"Skipped new mod file '{mod_rel_path}'.", "INFO", 1)
                    skipped_files_count += 1
                total_files_with_mod_changes += 1
                continue # Done with this file, move to next

            # Scenario 2: File exists in New Vanilla. Check if YOUR MOD changed it.
            elif os.path.exists(new_vanilla_abs_path):
                # Is there a corresponding Old Vanilla file?
                mod_file_exists_in_old_vanilla = os.path.exists(old_vanilla_abs_path)
                
                # Check if your mod file (M) is identical to Old Vanilla (O).
                # If so, your mod made no changes to this file, so just copy New Vanilla.
                if mod_file_exists_in_old_vanilla and filecmp.cmp(mod_abs_path, old_vanilla_abs_path, shallow=False):
                    log_message(f"File '{mod_rel_path}' has NO MOD CHANGES (identical to Old Vanilla). Copying New Vanilla.", "INFO")
                    os.makedirs(os.path.dirname(output_abs_path), exist_ok=True)
                    shutil.copy2(new_vanilla_abs_path, output_abs_path)
                    processed_files_count += 1
                    # total_files_with_mod_changes does NOT increment here as mod had no changes
                    continue # Done with this file, move to next

                # Scenario 3: Modded file is different from Old Vanilla (or Old Vanilla is missing, implying Mod's original file was new/changed)
                # This is where we need the 3-way diff merge.
                log_message(f"File '{mod_rel_path}' has MOD CHANGES (differs from Old Vanilla). Initiating 3-way merge.", "INFO")
                if process_single_file_merge(mod_rel_path, differ): # Pass the differ instance
                    processed_files_count += 1
                else:
                    skipped_files_count += 1
                total_files_with_mod_changes += 1
            
            # Scenario 4: File only in Old Vanilla (not in Mod Source, not in New Vanilla) - a vanilla deletion not impacted by your mod.
            # This file is typically not part of the mod update process.
            elif os.path.exists(old_vanilla_abs_path):
                log_message(f"File '{mod_rel_path}' exists only in Old Vanilla (or original mod), not in New Vanilla or current Mod. Skipping.", "DEBUG", 1)
                # No action needed, it was likely deleted by Paradox and you didn't have it either or removed it.
            else:
                log_message(f"File '{mod_rel_path}' not found in any relevant directories. Skipping.", "DEBUG", 1)


    log_message("\n" + ("-" * 30) + f" Final Summary ({script_version}) " + ("-" * 30), "HEAD")
    log_message(f"Total files processed: {processed_files_count}", "RSLT_DTL")
    log_message(f"Total files skipped/unmodified by script: {skipped_files_count}", "RSLT_DTL")
    log_message(f"Total files with mod-initiated changes (processed for merge): {total_files_with_mod_changes}", "RSLT_DTL")
    log_message("-" * (78), "HEAD")
    log_message(f"Script finished. Check the '{MOD_OUTPUT_DIR}' directory.", "HEAD")
    log_message("CRITICAL: Manually review ALL generated files. Use a diff tool!", "WARN ")

if __name__ == "__main__":
    script_version_print = "v13.1 - Enhanced File Filtering, Error Handling"
    print(f"--- Crusader Kings III Mod Updater Script ({script_version_print}) ---")
    print(f"MOD SOURCE:          '{MOD_SOURCE_DIR}'")
    print(f"OLD VANILLA REF:     '{OLD_VANILLA_DIR_REFERENCE}'")
    print(f"NEW VANILLA GAME:    '{GAME_VANILLA_DIR_NEW}'")
    print(f"OUTPUT TO:           '{MOD_OUTPUT_DIR}' (in CWD: '{SCRIPT_EXECUTION_DIR}')")
    print("-" * 80)
    main()