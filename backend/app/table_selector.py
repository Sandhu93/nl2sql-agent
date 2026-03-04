"""
Table description helpers for dynamic table selection.

Reads database_table_descriptions.csv to give the LLM plain-English
descriptions of every table so it can decide which ones to include in
the SQL-generation prompt without seeing the full schema.
"""

from pathlib import Path

import pandas as pd

# Path to the CSV that describes each table in plain English.
# Mounted into the container at /app/app/ via the backend volume.
_TABLE_DESCRIPTIONS_CSV = Path(__file__).parent / "database_table_descriptions.csv"


def get_table_details() -> str:
    """
    Return a formatted string listing every table name and its plain-English
    description, read from database_table_descriptions.csv.

    Used in the table-selection prompt so the LLM can decide which tables
    are relevant without seeing column definitions or sample rows.
    """
    df = pd.read_csv(_TABLE_DESCRIPTIONS_CSV)
    details = ""
    for _, row in df.iterrows():
        details += f"Table Name: {row['Table']}\nTable Description: {row['Description']}\n\n"
    return details


def get_table_names() -> list[str]:
    """Return the list of table names defined in the descriptions CSV."""
    return pd.read_csv(_TABLE_DESCRIPTIONS_CSV)["Table"].tolist()
