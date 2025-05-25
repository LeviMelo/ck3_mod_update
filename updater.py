# -----------------------------------------------------------------------------
# Crusader Kings III Mod Updater Script (v12.2 - Restored Core Parsers, Block Logic, Targeted Nested Fix)
# -----------------------------------------------------------------------------
# Author: AI Assistant (with extensive user feedback & apologies)
# Date: 2025-05-25
#
# Purpose:
# Compares a CK3 mod against an old vanilla version (its base) and a new
# vanilla version (target for update). It focuses on identifying entire
# top-level blocks that were added or modified by the user. For modified blocks,
# it allows the user to replace the new vanilla block with their version.
# It includes a specific, more granular mechanism for adding a known nested
# parameter (like 'diplomatic_range_mult' in 'character_modifier' within
# 'innovation_longboats'), attempting to place it correctly.
#
# Key Features:
# 1. Identifies user's changes by comparing mod source to old vanilla,
#    focusing on added files, added top-level entries, or modified top-level entries.
# 2. For each changed file:
#    a. Prompts user to process the ENTIRE FILE or skip it after a summary of block changes.
# 3. If user proceeds with a file:
#    a. For ADDED ENTRIES: Prompts to append the new entry (entire block).
#    b. For MODIFIED ENTRIES (General): Displays mod's block vs. new vanilla's block,
#       prompts user to replace new vanilla block with mod's version.
#    c. Special handling for 'innovation_longboats' to offer adding
#       'diplomatic_range_mult' into its 'character_modifier' sub-block.
# 4. Adds in-line comments to modified/added lines/blocks.
# 5. Creates a new output mod folder. Originals are NEVER modified.
# -----------------------------------------------------------------------------

import os
import re
import shutil
import filecmp
from datetime import datetime, timezone

# --- CONFIGURATION ---
MOD_SOURCE_DIR = r"C:\Users\Galaxy\Documents\Paradox Interactive\Crusader Kings III\mod\custom_changes"
OLD_VANILLA_DIR_REFERENCE = r"C:\Users\Galaxy\LEVI\jupyter\ck3_mod_update\old_ver"
GAME_VANILLA_DIR_NEW = r"C:\Program Files (x86)\Steam\steamapps\common\Crusader Kings III\game"
OUTPUT_SUBFOLDER_NAME = "updated_mod_v12_2_core_fix"
FOLDERS_TO_PROCESS = ["common", "events"]
# --- END CONFIGURATION ---

# --- Global Variables ---
SCRIPT_EXECUTION_DIR = os.getcwd()
MOD_OUTPUT_DIR = os.path.join(SCRIPT_EXECUTION_DIR, OUTPUT_SUBFOLDER_NAME)

# --- Interaction Control Flags ---
AUTO_PROCESS_ALL_FILES_SESSION = False
AUTO_APPLY_ALL_CHANGES_THIS_FILE = False
AUTO_APPLY_ALL_CHANGES_SESSION = False

# --- Helper Functions ---
def log_message(message, level="INFO", indent=0):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    indent_space = "  " * indent; level_str = f"[{level.upper():<7}]"
    print(f"{timestamp} {level_str} {indent_space}{message}")

def get_user_confirmation(prompt_message, prompt_level="param_apply", default_choice_on_conflict=None):
    global AUTO_PROCESS_ALL_FILES_SESSION, AUTO_APPLY_ALL_CHANGES_THIS_FILE, AUTO_APPLY_ALL_CHANGES_SESSION
    if prompt_level == "file_process_summary" and AUTO_PROCESS_ALL_FILES_SESSION: return "yes"
    if prompt_level in ["block_replace", "entry_add", "param_add_specific"]:
        if AUTO_APPLY_ALL_CHANGES_THIS_FILE: return "yes"
        if AUTO_APPLY_ALL_CHANGES_SESSION: return "yes"

    base_options_text = "(y[es]/n[o]"
    if prompt_level == "file_process_summary":
        options_text = f"{base_options_text}/S[kip ALL file summaries & auto-process files])"
    elif prompt_level in ["block_replace", "entry_add", "param_add_specific"]:
        options_text = f"{base_options_text}/a[pply ALL for current file]/s[kip ALL change confirmations for session])"
    else: options_text = f"{base_options_text})"

    while True:
        full_prompt = f"[PROMPT ] {prompt_message} {options_text}: "
        response = input(full_prompt).strip().lower()
        if response in ["yes", "y"]: return "yes"
        if response in ["no", "n"]: return "no"
        # Removed default_choice_on_conflict for now as block prompts are simpler y/n
        if prompt_level == "file_process_summary":
            if response in ["s", "skipallfiles"]: AUTO_PROCESS_ALL_FILES_SESSION = True; return "yes"
        elif prompt_level in ["block_replace", "entry_add", "param_add_specific"]:
            if response in ["a", "applyall"]: AUTO_APPLY_ALL_CHANGES_THIS_FILE = True; return "yes"
            if response in ["s", "skipall", "skipallconfirmations"]: AUTO_APPLY_ALL_CHANGES_SESSION = True; return "yes"
        print("[ERROR  ] Invalid input.")

def get_file_lines(filepath, context="reading file"):
    try:
        with open(filepath, 'r', encoding='utf-8-sig') as f: return f.readlines()
    except UnicodeDecodeError:
        try:
            with open(filepath, 'r', encoding='utf-8') as f: return f.readlines()
        except Exception as e_inner: log_message(f"ReadErr '{filepath}' ({context}): {e_inner}", "ERROR",1); return None
    except FileNotFoundError: log_message(f"NotFound '{filepath}' ({context})", "WARN ",1); return None
    except Exception as e: log_message(f"ReadErr '{filepath}' ({context}): {e}", "ERROR",1); return None

def write_file_lines(filepath, lines):
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'w', encoding='utf-8-sig') as f: f.writelines(lines)
        return True
    except Exception as e: log_message(f"WriteErr '{filepath}': {e}", "ERROR",1); return False

# --- START OF RESTORED/CORRECTED PARSING HELPER FUNCTIONS ---
def parse_parameter_line(line_text):
    stripped_line = line_text.strip()
    original_comment = ""
    if '#' in stripped_line:
        parts = stripped_line.split('#', 1)
        effective_line_part = parts[0].strip()
        if len(parts) > 1: original_comment = parts[1].strip()
    else:
        effective_line_part = stripped_line
    
    if not effective_line_part: # Line was empty or only a comment
        return None, None, False, original_comment

    # Regex allows @ in keys, and dots/dashes.
    match = re.match(r'^\s*([@\w\.-]+)\s*=\s*(.+)$', effective_line_part)
    if match:
        key, value_str = match.group(1), match.group(2).strip()
        # A value is "not simple" if it IS a block start or a full block itself.
        is_simple = True 
        if value_str == "{": is_simple = False 
        elif value_str.startswith("{") and value_str.endswith("}") and value_str != "{}":
            # If it contains internal structure (more braces or assignments), it's not simple for value replacement
            if '=' in value_str[1:-1] or '{' in value_str[1:-1]: is_simple = False
        return key, value_str, is_simple, original_comment
    return None, None, False, original_comment # Not a 'key = value' line

def find_entry_block_indices(entry_name_or_regex, content_lines, start_search_from_idx=0, is_regex=False):
    """Finds start/end line indices of an entry: entry_name = { ... } or regex match for key"""
    block_start_idx, brace_level = -1, 0
    
    entry_start_regex = None
    if not is_regex:
        entry_start_regex = re.compile(r"^\s*" + re.escape(entry_name_or_regex) + r"\s*=\s*\{")
    else: 
        entry_start_regex = entry_name_or_regex

    for i in range(start_search_from_idx, len(content_lines)):
        line = content_lines[i]
        stripped_line = line.strip() 
        
        if block_start_idx == -1:
            match_obj = entry_start_regex.match(stripped_line) 
            if match_obj:
                block_start_idx = i
                # Correctly count braces only from the line where the block starts
                brace_level = line.count('{') - line.count('}')
                if brace_level <= 0: # Handles single-line blocks or errors
                    if stripped_line.endswith("}") and line.count('{') == line.count('}'): 
                        return block_start_idx, i 
                    block_start_idx = -1 # Reset, not a valid start for a multi-line block
        elif block_start_idx != -1: # Inside a block
            brace_level += line.count('{')
            brace_level -= line.count('}')
            if brace_level <= 0: 
                return block_start_idx, i
    return None, None

def get_parameters_within_block(content_lines, block_start_idx, block_end_idx):
    params = {} # key: (value_str, original_comment_str)
    if block_start_idx is None or block_start_idx >= block_end_idx: return params
    for i in range(block_start_idx + 1, block_end_idx): # Iterate lines *inside* the main block braces
        param_key, param_value, is_simple, comment = parse_parameter_line(content_lines[i])
        if param_key and is_simple: # Only store if identified as a simple parameter
            params[param_key] = (param_value, comment)
    return params
# --- END RESTORED PARSING HELPER FUNCTIONS ---


# --- Phase 1: Identify Changed/Added Top-Level Blocks (Mod vs OLD VANILLA) ---
def identify_block_level_actions():
    log_message("Phase 1: Identifying changed/added top-level blocks (Mod vs OLD VANILLA)...", "PHASE")
    all_potential_actions = {}
    entry_regex = re.compile(r"^\s*([@\w\.-]+)\s*=\s*\{") # Includes @ for keys
    files_with_actions = 0

    for folder_name in FOLDERS_TO_PROCESS:
        mod_folder = os.path.join(MOD_SOURCE_DIR, folder_name)
        old_vanilla_folder = os.path.join(OLD_VANILLA_DIR_REFERENCE, folder_name)
        log_message(f"Scanning folder: '{folder_name}'", "INFO", 1)

        if not os.path.isdir(mod_folder): continue

        for root, _, files in os.walk(mod_folder):
            for filename in files:
                if not filename.lower().endswith(".txt"): continue
                
                mod_rel_path = os.path.relpath(os.path.join(root, filename), MOD_SOURCE_DIR)
                mod_abs_path = os.path.join(MOD_SOURCE_DIR, mod_rel_path)
                old_vanilla_abs_path = os.path.join(OLD_VANILLA_DIR_REFERENCE, mod_rel_path)
                
                file_actions_list = []
                mod_lines = get_file_lines(mod_abs_path, f"reading mod file '{mod_rel_path}'")
                if not mod_lines: continue

                if not os.path.exists(old_vanilla_abs_path) or not os.path.isdir(old_vanilla_folder) :
                    all_potential_actions[mod_rel_path] = {
                        'file_type': 'new_by_mod', 'content_if_new': "".join(mod_lines)
                    }
                    log_message(f"File '{mod_rel_path}' is NEW in mod (or Old Vanilla path missing).", "DETAIL", 2)
                    files_with_actions +=1
                    continue 
                
                if filecmp.cmp(mod_abs_path, old_vanilla_abs_path, shallow=False):
                    log_message(f"File '{mod_rel_path}' is identical to Old Vanilla. No changes by you.", "DEBUG", 2)
                    continue

                log_message(f"File '{mod_rel_path}' differs from Old Vanilla. Analyzing entries...", "INFO", 2)
                old_vanilla_lines = get_file_lines(old_vanilla_abs_path, f"reading old vanilla '{mod_rel_path}'")
                if not old_vanilla_lines: continue

                mod_entries = {} 
                idx = 0
                while idx < len(mod_lines):
                    match = entry_regex.match(mod_lines[idx].strip())
                    if match:
                        entry_name = match.group(1)
                        e_start, e_end = find_entry_block_indices(entry_name, mod_lines, idx)
                        if e_start is not None:
                            mod_entries[entry_name] = mod_lines[e_start : e_end+1]
                            idx = e_end + 1
                            continue
                    idx += 1
                
                old_van_entries = {}
                idx = 0
                while idx < len(old_vanilla_lines):
                    match = entry_regex.match(old_vanilla_lines[idx].strip())
                    if match:
                        entry_name = match.group(1)
                        e_start, e_end = find_entry_block_indices(entry_name, old_vanilla_lines, idx)
                        if e_start is not None:
                            old_van_entries[entry_name] = old_vanilla_lines[e_start : e_end+1]
                            idx = e_end + 1
                            continue
                    idx += 1

                for entry_name, mod_block_lines in mod_entries.items():
                    if entry_name not in old_van_entries:
                        file_actions_list.append({
                            'type': 'entry_added', 'entry_name': entry_name,
                            'mod_block_lines': mod_block_lines
                        })
                    elif mod_block_lines != old_van_entries[entry_name]: 
                        file_actions_list.append({
                            'type': 'entry_modified', 'entry_name': entry_name,
                            'mod_block_lines': mod_block_lines,
                            'old_van_block_lines': old_van_entries[entry_name] # Keep for reference if needed
                        })
                
                if file_actions_list:
                    all_potential_actions[mod_rel_path] = {
                        'file_type': 'modified_vanilla', 'changes': file_actions_list
                    }
                    files_with_actions +=1
                else: 
                    log_message(f"File '{mod_rel_path}' differs textually but no top-level block changes/additions parsed.", "WARN ", 3)
    
    log_message(f"Phase 1 Summary: Identified block-level actions for {files_with_actions} files.", "PHASE")
    return all_potential_actions

# --- Phase 2: Apply Block-Level Changes and Targeted Param Adds to NEW VANILLA ---
def apply_block_changes_and_targeted_adds(potential_actions_map):
    log_message("Phase 2: Applying block changes and targeted param adds...", "PHASE")
    summary = {k: 0 for k in [
        "files_written", "blocks_replaced_by_mod", "blocks_kept_nv",
        "entries_appended_by_mod", "entries_skipped_by_user",
        "custom_files_copied", "custom_files_skipped_user",
        "targeted_param_added", "targeted_param_add_skipped", "targeted_param_modified", "targeted_param_modification_skipped",
        "warn_nv_file_missing", "warn_entry_missing_nv_for_mod_block", "warn_sub_block_missing_nv",
        "files_mod_eq_nv_skipped_auto", "files_skipped_at_file_prompt"
    ]}
    global AUTO_APPLY_ALL_CHANGES_THIS_FILE

    if not potential_actions_map:
        log_message("No potential actions from Phase 1.", "INFO", 1); return summary

    for mod_rel_path, action_details in potential_actions_map.items():
        AUTO_APPLY_ALL_CHANGES_THIS_FILE = False
        log_message(f"Reviewing File: '{mod_rel_path}'", "HEADER")
        output_abs_path = os.path.join(MOD_OUTPUT_DIR, mod_rel_path) # Defined here

        if action_details['file_type'] == 'new_by_mod':
            prompt_msg = f"File '{mod_rel_path}' was created by your mod. Copy to updated mod?"
            if get_user_confirmation(prompt_msg, prompt_level="entry_add") == "yes":
                content_str = action_details['content_if_new']
                if write_file_lines(output_abs_path, content_str.splitlines(True)):
                    summary["files_written"] += 1; summary["custom_files_copied"] += 1
            else: summary["custom_files_skipped_user"] +=1
            continue

        new_vanilla_abs_path = os.path.join(GAME_VANILLA_DIR_NEW, mod_rel_path)
        mod_source_abs_path = os.path.join(MOD_SOURCE_DIR, mod_rel_path)

        if not os.path.exists(new_vanilla_abs_path):
            log_message(f"NEW VANILLA for '{mod_rel_path}' NOT found. Skipping.", "WARN ", 1)
            summary["warn_nv_file_missing"] += 1; continue
        
        if os.path.exists(mod_source_abs_path) and filecmp.cmp(mod_source_abs_path, new_vanilla_abs_path, shallow=False):
            log_message(f"Your modded file '{mod_rel_path}' is ALREADY IDENTICAL to NEW VANILLA. Skipping.", "INFO", 1)
            summary["files_mod_eq_nv_skipped_auto"] +=1; continue

        new_vanilla_lines_base = get_file_lines(new_vanilla_abs_path, "reading new vanilla base")
        if not new_vanilla_lines_base: continue

        changes_in_file = action_details.get('changes', [])
        if not changes_in_file:
            log_message(f"No specific block changes identified by Phase 1 for '{mod_rel_path}', though files differ. Manual review advised.", "INFO", 1)
            continue

        log_message(f"File '{mod_rel_path}' has ~{len(changes_in_file)} modified/added top-level entries (Mod vs OldVanilla):", "INFO", 1)
        for i, change in enumerate(changes_in_file):
            log_message(f"  Change {i+1}: Type='{change['type']}', Entry='{change['entry_name']}'", "DETAIL", 2)
        
        prompt_file_msg = f"Process file '{mod_rel_path}' with {len(changes_in_file)} entry changes detailed above?"
        if get_user_confirmation(prompt_file_msg, prompt_level="file_process_summary") == "no":
            log_message(f"Skipping file '{mod_rel_path}' by user choice.", "INFO", 1)
            summary["files_skipped_at_file_prompt"] += 1; continue
        
        output_lines = list(new_vanilla_lines_base) # Start with new vanilla for modifications
        file_was_modified_by_script = False

        for change_item in changes_in_file:
            entry_name = change_item['entry_name']
            action_type = change_item['type']

            if action_type == 'entry_added':
                mod_block_lines = change_item['mod_block_lines']
                prompt_add = f"Entry '{entry_name}': Your mod ADDED this. Append to New Vanilla '{mod_rel_path}'?"
                nv_entry_s_check, _ = find_entry_block_indices(entry_name, output_lines)
                if nv_entry_s_check is not None:
                    prompt_add += " (WARNING: Name conflicts with an existing New Vanilla entry!)"

                if get_user_confirmation(prompt_add, prompt_level="entry_add") == "yes":
                    log_message(f"Appending your new entry block '{entry_name}'.", "DETAIL", 2)
                    if output_lines and not output_lines[-1].endswith(('\n','\r')): output_lines.append('\n')
                    output_lines.append(f"\n# --- MODDED: Entry '{entry_name}' Added By Mod ---\n")
                    output_lines.extend(mod_block_lines) # mod_block_lines already have newlines
                    file_was_modified_by_script = True; summary["entries_appended_by_mod"] += 1
                else: summary["entries_skipped_user"] +=1

            elif action_type == 'entry_modified':
                mod_block_lines_for_entry = change_item['mod_block_lines']
                
                # Find this entry in the current output_lines (which starts as new_vanilla_lines_base)
                out_nv_entry_s, out_nv_entry_e = find_entry_block_indices(entry_name, output_lines)
                
                if out_nv_entry_s is None:
                    log_message(f"Entry '{entry_name}' you modified is MISSING in New Vanilla structure of '{mod_rel_path}'. Cannot merge block.", "WARN ", 2)
                    summary["warn_entry_missing_nv_for_mod_block"] += 1; continue

                current_nv_block_lines_in_output = output_lines[out_nv_entry_s : out_nv_entry_e+1]

                if mod_block_lines_for_entry == current_nv_block_lines_in_output: # Content comparison
                    log_message(f"Entry '{entry_name}': Your modded block is identical to this New Vanilla block in current output. No change.", "INFO", 2)
                    continue

                log_message(f"Entry '{entry_name}': Your mod's block differs from New Vanilla's.", "INFO", 2)
                
                # --- SPECIAL HANDLING for innovation_longboats -> character_modifier -> diplomatic_range_mult ---
                handled_by_special_logic = False
                if entry_name == 'innovation_longboats':
                    log_message(f"Checking for specific 'diplomatic_range_mult' tweak within '{entry_name}'.", "DETAIL", 3)
                    target_sub_block_key = 'character_modifier'
                    param_to_add_key = 'diplomatic_range_mult'
                    
                    # 1. Check if your mod's innovation_longboats has char_mod with diplo_range
                    mod_il_cm_s, mod_il_cm_e = find_entry_block_indices(target_sub_block_key, mod_block_lines_for_entry)
                    your_diplo_val_from_mod = None
                    if mod_il_cm_s is not None:
                        mod_cm_params = get_parameters_within_block(mod_block_lines_for_entry, mod_il_cm_s, mod_il_cm_e)
                        if param_to_add_key in mod_cm_params:
                            your_diplo_val_from_mod = mod_cm_params[param_to_add_key][0]

                    if your_diplo_val_from_mod is not None: # Your mod wants to set/add this param
                        # 2. Find character_modifier in current output_lines (within the innovation_longboats block)
                        # We search from the start of the innovation_longboats block in output_lines
                        cm_out_s, cm_out_e = find_entry_block_indices(target_sub_block_key, output_lines, out_nv_entry_s) 
                        
                        if cm_out_s is not None and cm_out_s > out_nv_entry_s and cm_out_e < out_nv_entry_e : # Ensure sub-block is within parent
                            out_cm_params = get_parameters_within_block(output_lines, cm_out_s, cm_out_e)
                            current_diplo_val_in_nv_subblock = out_cm_params.get(param_to_add_key, (None,None))[0]

                            log_message(f"  Sub-block '{target_sub_block_key}', Param '{param_to_add_key}':", "INFO", 4)
                            log_message(f"    Your Mod wants : '{your_diplo_val_from_mod}'", "INFO", 5)
                            log_message(f"    New Vanilla has: '{current_diplo_val_in_nv_subblock if current_diplo_val_in_nv_subblock is not None else "__ABSENT__"}'", "INFO", 5)

                            if current_diplo_val_in_nv_subblock is None: # Absent in NV sub-block, your mod adds it
                                prompt_add_specific = f"Add '{param_to_add_key} = {your_diplo_val_from_mod}' to '{target_sub_block_key}' inside '{entry_name}'?"
                                if get_user_confirmation(prompt_add_specific, prompt_level="param_add_specific") == "yes":
                                    indent = "      " # Default
                                    if cm_out_e > cm_out_s + 1: # If char_mod block not empty
                                        m = re.match(r"(\s*)", output_lines[cm_out_s+1]); 
                                        if m: indent = m.group(1)
                                    new_line = f"{indent}{param_to_add_key} = {your_diplo_val_from_mod} # MODDED: Added by Mod (OV likely N/A here, NV=Absent)\n"
                                    output_lines.insert(cm_out_e, new_line) # Insert before closing '}' of character_modifier
                                    file_was_modified_by_script = True; summary["targeted_param_added"] +=1
                                    handled_by_special_logic = True
                                    log_message(f"Added '{param_to_add_key}' to '{target_sub_block_key}'.", "SUCCESS", 5)
                                else: summary["targeted_param_add_skipped"] +=1
                            elif your_diplo_val_from_mod != current_diplo_val_in_nv_subblock: # Exists in NV but different
                                prompt_mod_specific = f"Change '{param_to_add_key}' in '{target_sub_block_key}' from NV '{current_diplo_val_in_nv_subblock}' to YourMod '{your_diplo_val_from_mod}'?"
                                if get_user_confirmation(prompt_mod_specific, prompt_level="param_add_specific") == "yes": # Reusing param_add_specific options
                                    # Find the line and modify it
                                    for l_idx in range(cm_out_s + 1, cm_out_e):
                                        p_k, _, _, p_c = parse_parameter_line(output_lines[l_idx])
                                        if p_k == param_to_add_key:
                                            leading_ws = re.match(r"(\s*)", output_lines[l_idx]).group(1)
                                            cmt = f" # MODDED: OV=N/A, NV='{current_diplo_val_in_nv_subblock}', Applied='{your_diplo_val_from_mod}'"
                                            output_lines[l_idx] = f"{leading_ws}{p_key} = {your_diplo_val_from_mod}{(' '+p_c) if p_c else ''}{cmt}\n"
                                            file_was_modified_by_script = True; summary["targeted_param_modified"] +=1 # New summary key
                                            handled_by_special_logic = True
                                            log_message(f"Modified '{param_to_add_key}' in '{target_sub_block_key}'.", "SUCCESS", 5)
                                            break
                                else: summary["targeted_param_modification_skipped"] +=1 # New summary key
                            else: # Values match
                                log_message(f"'{param_to_add_key}' in '{target_sub_block_key}' already matches your mod. No change.", "SUCCESS", 5)
                                handled_by_special_logic = True # No change, but special case was checked.
                        else: # Your mod doesn't have the specific param, so no special action
                            log_message(f"Your mod's '{entry_name}' doesn't have '{param_to_add_key}' in '{target_sub_block_key}'. Skipping special add.", "DEBUG", 4)
                    else: # Character_modifier not found in New Vanilla's innovation_longboats
                        log_message(f"Sub-block '{target_sub_block_key}' not found in New Vanilla '{entry_name}'. Cannot apply specific tweak.", "WARN ", 4)
                        summary["warn_sub_block_missing_nv"] +=1


                if not handled_by_special_logic: # General block replacement prompt
                    log_message("  --- YOUR MODDED BLOCK (first 5 lines) ---", "DETAIL", 3)
                    for l_idx,l_content in enumerate(mod_block_lines_for_entry[:5]): print(f"      {l_content.rstrip()}")
                    if len(mod_block_lines_for_entry) > 5: print("      ...")
                    log_message("  --- NEW VANILLA BLOCK (first 5 lines) ---", "DETAIL", 3)
                    for l_idx,l_content in enumerate(current_nv_block_lines_in_output[:5]): print(f"      {l_content.rstrip()}")
                    if len(current_nv_block_lines_in_output) > 5: print("      ...")
                    
                    prompt_block = f"Entry '{entry_name}': Replace New Vanilla block with YOUR mod's version?"
                    if get_user_confirmation(prompt_block, prompt_level="block_replace") == "yes":
                        # Replace the block in output_lines
                        # Important: out_nv_entry_s, out_nv_entry_e were for original new_vanilla_lines_base.
                        # If output_lines was modified by a previous entry addition, these indices are stale.
                        # We must re-find the block in the current `output_lines` state.
                        current_out_nv_entry_s, current_out_nv_entry_e = find_entry_block_indices(entry_name, output_lines)
                        if current_out_nv_entry_s is not None:
                            del output_lines[current_out_nv_entry_s : current_out_nv_entry_e+1]
                            comment_prefix = [f"# --- MODDED: Block '{entry_name}' REPLACED by mod version ---\n"]
                            formatted_mod_block_lines = [l if l.endswith('\n') else l + '\n' for l in mod_block_lines_for_entry]
                            output_lines[current_out_nv_entry_s:current_out_nv_entry_s] = comment_prefix + formatted_mod_block_lines
                            file_was_modified_by_script = True; summary["blocks_replaced_by_mod"] +=1
                            log_message(f"Replaced block '{entry_name}'.", "SUCCESS", 3)
                        else: log_message(f"ERROR: Could not find '{entry_name}' in current output_lines to replace.", "ERROR", 3)
                    else:
                        summary["blocks_skipped_user"] +=1
                        log_message(f"Kept New Vanilla block for '{entry_name}'.", "INFO", 3)
        
        if file_was_modified_by_script:
            if write_file_lines(output_abs_path, output_lines):
                summary["files_written"] += 1
                log_message(f"Written '{output_abs_path}' to output.", "SUCCESS", 1)
        else:
            log_message(f"No confirmed changes written to output for '{mod_rel_path}'.", "INFO", 1)

    log_message(f"Phase 2 Summary.", "PHASE"); return summary

# --- Main Script Execution ---
def main():
    script_version = "v12.2 - Restored Core Parsers & Targeted Nested Fix"
    log_message(f"Script started. CK3 Mod Updater ({script_version}).", "HEAD")
    global AUTO_PROCESS_ALL_FILES_SESSION, AUTO_APPLY_ALL_CHANGES_THIS_FILE, AUTO_APPLY_ALL_CHANGES_SESSION
    AUTO_PROCESS_ALL_FILES_SESSION, AUTO_APPLY_ALL_CHANGES_THIS_FILE, AUTO_APPLY_ALL_CHANGES_SESSION = False, False, False
    
    paths_ok = True
    for p, n in [("MOD_SOURCE_DIR",MOD_SOURCE_DIR), ("OLD_VANILLA_DIR_REFERENCE",OLD_VANILLA_DIR_REFERENCE), ("GAME_VANILLA_DIR_NEW",GAME_VANILLA_DIR_NEW)]:
        if not os.path.isdir(n): log_message(f"{p} not found: '{n}'. Exiting.", "FATAL"); paths_ok=False
    if not paths_ok: return

    log_message(f"Output: '{MOD_OUTPUT_DIR}'", "INFO")
    log_message("Output dir will be DELETED/RECREATED.", "WARN ")
    if get_user_confirmation(f"Proceed? (Deletes '{OUTPUT_SUBFOLDER_NAME}')", prompt_level="initial_script_start") == "no":
        log_message("Cancelled.", "INFO"); return
    if os.path.exists(MOD_OUTPUT_DIR):
        log_message(f"Removing '{MOD_OUTPUT_DIR}'...", "DETAIL")
        try: shutil.rmtree(MOD_OUTPUT_DIR)
        except Exception as e: log_message(f"Could not remove output dir: {e}. Exiting.", "FATAL"); return
    try: os.makedirs(MOD_OUTPUT_DIR, exist_ok=True)
    except Exception as e: log_message(f"Could not create output dir: {e}. Exiting.", "FATAL"); return
    log_message(f"Output dir ready: '{MOD_OUTPUT_DIR}'", "SUCCESS")

    potential_actions = identify_block_level_actions()
    
    if not potential_actions:
        log_message("No potential actions from your mod. Output mod empty.", "INFO")
    else:
        final_summary = apply_block_changes_and_targeted_adds(potential_actions)
        log_message("\n" + ("-" * 30) + f" Final Summary ({script_version}) " + ("-" * 30), "HEAD")
        for key, value in final_summary.items():
            log_message(f"  {key.replace('_', ' ').capitalize()}: {value}", "RSLT_DTL")
        log_message("-" * (78), "HEAD")

    log_message(f"Script finished. Check the '{MOD_OUTPUT_DIR}' directory.", "HEAD")
    log_message("CRITICAL: Manually review ALL generated files. Use a diff tool!", "WARN ")

if __name__ == "__main__":
    script_version_print = "v12.2 - Restored Core Parsers, Block Logic, Targeted Nested Fix"
    print(f"--- Crusader Kings III Mod Updater Script ({script_version_print}) ---")
    print(f"MOD SOURCE:          '{MOD_SOURCE_DIR}'")
    print(f"OLD VANILLA REF:     '{OLD_VANILLA_DIR_REFERENCE}'")
    print(f"NEW VANILLA GAME:    '{GAME_VANILLA_DIR_NEW}'")
    print(f"OUTPUT TO:           '{MOD_OUTPUT_DIR}' (in CWD: '{SCRIPT_EXECUTION_DIR}')")
    print("-" * 80)
    main()