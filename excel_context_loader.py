"""
Excel-based Context Variable Mapping Loader
Loads context variable mappings from Excel file for AWS to GCP migration.
"""

import pandas as pd
import os
from typing import Dict, Tuple, Set


# Excel file path
EXCEL_CONTEXT_FILE = r"D:\context-sheets\context_c360.xlsx"


def load_excel_context_mappings() -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str], Dict[str, str]]:
    """
    Load context variable mappings from Excel file.

    Returns:
        Tuple containing:
        - context_renames: {old_context_name: new_context_name}
        - variable_renames: {old_variable_name: new_variable_name}
        - context_variable_map: {context.old_var: context.new_var}
        - context_to_project_map: {context_name: {dataset_var: project_var}}
    """
    if not os.path.exists(EXCEL_CONTEXT_FILE):
        print(f"⚠️  Excel file not found: {EXCEL_CONTEXT_FILE}")
        print(f"   Using hardcoded mappings from config.py")
        return {}, {}, {}, {}

    try:
        df = pd.read_excel(EXCEL_CONTEXT_FILE)

        # Validate required columns
        required_cols = ['Context', 'Variable_name', 'New_Context', 'New_Variable_Name',
                        'No_Change_Context', 'No_Change_Var_Name']
        missing_cols = [col for col in required_cols if col not in df.columns]
        if missing_cols:
            raise ValueError(f"Excel file missing required columns: {missing_cols}")

        context_renames = {}
        variable_renames = {}
        context_variable_map = {}
        context_to_project_map = {}

        for _, row in df.iterrows():
            old_context = str(row['Context'])
            new_context = str(row['New_Context'])
            old_var = str(row['Variable_name'])
            new_var = str(row['New_Variable_Name'])
            no_change_ctx = row['No_Change_Context']
            no_change_var = row['No_Change_Var_Name']

            # Track context renames
            if not no_change_ctx and old_context != new_context:
                context_renames[old_context] = new_context

            # Track variable renames
            if not no_change_var and old_var != new_var:
                variable_renames[old_var] = new_var
                # Also track full context.variable format
                context_variable_map[f"context.{old_var}"] = f"context.{new_var}"

        # Build context-to-project mapping for dataset prefixing
        # Group by New_Context to find Project and Dataset pairs
        for new_context in df['New_Context'].unique():
            ctx_data = df[df['New_Context'] == new_context]

            # Find project variable(s) in this context
            project_vars = ctx_data[ctx_data['New_Variable_Name'].str.contains('Project', na=False, case=False)]
            dataset_vars = ctx_data[ctx_data['New_Variable_Name'].str.contains('Dataset', na=False, case=False)]

            if len(project_vars) > 0 and len(dataset_vars) > 0:
                context_to_project_map[new_context] = {}

                # For most contexts, there's one project for all datasets
                if len(project_vars) == 1:
                    project_var = project_vars.iloc[0]['New_Variable_Name']
                    for _, ds_row in dataset_vars.iterrows():
                        dataset_var = ds_row['New_Variable_Name']
                        context_to_project_map[new_context][dataset_var] = project_var

                # For BQ_contexts with multiple projects, use gcp_compute_project
                elif 'BQ_contexts' in new_context or 'bq_contexts' in new_context:
                    for _, ds_row in dataset_vars.iterrows():
                        dataset_var = ds_row['New_Variable_Name']
                        context_to_project_map[new_context][dataset_var] = 'gcp_compute_project'

                # For other multi-project contexts, try to match by prefix
                else:
                    for _, ds_row in dataset_vars.iterrows():
                        dataset_var = ds_row['New_Variable_Name']
                        # Extract prefix from dataset variable
                        prefix = dataset_var.replace('_Dataset', '').replace('_Dataset_Stage', '')
                        # Look for matching project variable
                        expected_project = f"{prefix}_Project"
                        matching_projects = project_vars[project_vars['New_Variable_Name'] == expected_project]
                        if len(matching_projects) > 0:
                            context_to_project_map[new_context][dataset_var] = expected_project
                        elif len(project_vars) == 1:
                            # Fallback to the only project available
                            context_to_project_map[new_context][dataset_var] = project_vars.iloc[0]['New_Variable_Name']

        print(f"✓ Loaded {len(context_renames)} context renames from Excel")
        print(f"✓ Loaded {len(variable_renames)} variable renames from Excel")
        print(f"✓ Loaded {len(context_variable_map)} context.variable mappings from Excel")
        print(f"✓ Loaded {len(context_to_project_map)} context-to-project mappings from Excel")

        return context_renames, variable_renames, context_variable_map, context_to_project_map

    except Exception as e:
        print(f"⚠️  Error loading Excel file: {e}")
        print(f"   Using hardcoded mappings from config.py")
        return {}, {}, {}, {}


def get_new_context_name(old_context: str, context_renames: Dict[str, str]) -> str:
    """Get new context name from mapping, or return original if no mapping exists."""
    return context_renames.get(old_context, old_context)


def get_new_variable_name(old_variable: str, variable_renames: Dict[str, str]) -> str:
    """Get new variable name from mapping, or return original if no mapping exists."""
    return variable_renames.get(old_variable, old_variable)


def get_project_for_dataset(context_name: str, dataset_var: str,
                            context_to_project_map: Dict[str, Dict[str, str]]) -> str:
    """
    Get the project variable that should be prepended to a dataset variable.

    Args:
        context_name: The context name (e.g., 'BQ_CustDB_MKT')
        dataset_var: The dataset variable name (e.g., 'BQ_CustDB_MKT_Dataset')
        context_to_project_map: Mapping from context to {dataset: project} pairs

    Returns:
        The project variable name to use, or None if not found
    """
    if context_name in context_to_project_map:
        return context_to_project_map[context_name].get(dataset_var)
    return None


# Load mappings on module import
CONTEXT_RENAMES, VARIABLE_RENAMES, CONTEXT_VARIABLE_MAP, CONTEXT_TO_PROJECT_MAP = load_excel_context_mappings()
