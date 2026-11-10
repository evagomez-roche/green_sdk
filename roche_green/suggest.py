import os
import pandas as pd
import sys

def load_patterns_db():
    # Detect exactly where this script is installed
    current_dir = os.path.dirname(os.path.abspath(__file__))
    # Build the absolute path to the Excel file
    file_path = os.path.join(current_dir, "GSF Patterns - Patterns.xlsx")
    
    try:
        # Read the entire file without headers. We use sheet_name=0 to ensure
        # it reads the first sheet regardless of its name.
        df = pd.read_excel(file_path, sheet_name=0, header=None)
        
        # Search the first column (index 0) for the cell containing "Pattern Name"
        mask = df[0].astype(str).str.contains("Pattern Name", case=False, na=False)
        header_idx = df[mask].index.tolist()
        
        if not header_idx:
            print(f"❌ Error: The 'Pattern Name' header was not found in the first column of the file.")
            sys.exit(1)
            
        real_header_row = header_idx[0]
        
        # Assign that row as the actual column headers
        df.columns = df.iloc[real_header_row]
        
        # Keep only the data below that header
        df = df.iloc[real_header_row + 1:].reset_index(drop=True)
        
        # Drop any empty rows based on the Pattern Name column
        df = df.dropna(subset=['Pattern Name'])
        
        return df
        
    except FileNotFoundError:
        print(f"❌ Error: File '{file_path}' not found. Ensure it is included in the package folder.")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error loading the patterns database: {e}")
        sys.exit(1)

def run_triage():
    print("\n🌿 Roche Green Coding - Optimization Assistant (Powered by GSF) 🌿")
    print("Answer a few quick questions to get tailored architecture patterns for your project.\n")
    
    df_patterns = load_patterns_db()

    # Extract unique Personas
    all_personas_raw = df_patterns['Persona'].dropna().str.split(',').explode().str.strip().unique()
    all_personas = sorted([p for p in all_personas_raw if p])
    
    print("Which role(s) best describe your focus on this project? (You can select multiple)")
    for i, p in enumerate(all_personas, 1):
        print(f"  {i}. {p}")
    print(f"  {len(all_personas) + 1}. Other / Not Listed")
    
    selected_personas = []
    user_input = input("\nEnter the numbers of your roles (e.g., 1,3) or type 'all': ").strip().lower()
    
    if user_input == 'all':
        selected_personas = all_personas
    else:
        try:
            indices = [int(x.strip()) - 1 for x in user_input.split(',')]
            for idx in indices:
                if 0 <= idx < len(all_personas):
                    selected_personas.append(all_personas[idx])
                elif idx == len(all_personas):
                    custom_role = input("Please specify your role: ").strip()
                    selected_personas.append(custom_role)
        except ValueError:
            print("Invalid input detected. Defaulting to 'Software Engineer'.")
            selected_personas = ["Software Engineer"]

    if not selected_personas:
        selected_personas = ["Software Engineer"]

    print(f"\n✅ Selected Roles: {', '.join(selected_personas)}")

    # AI context question
    is_ai_project = input("\nDoes this project involve Artificial Intelligence, LLMs, or Heavy Machine Learning? (Y/N): ").strip().upper() == 'Y'

    print("\n🔍 Analyzing high-impact patterns for your profile...")
    
    # Filter the DataFrame
    escaped_personas = [pd.Series([p]).str.replace(r'([.^+*?|()\[\]{}\\])', r'\\\1', regex=True).iloc[0] for p in selected_personas]
    role_regex = '|'.join(escaped_personas)
    
    mask_role = df_patterns['Persona'].fillna('').str.contains(role_regex, case=False, na=False)
    df_filtered = df_patterns[mask_role].copy()
    
    # Force inclusion of AI patterns if applicable
    if is_ai_project and "AI/ML Engineer" not in selected_personas:
        ai_mask = df_patterns['Persona'].fillna('').str.contains("AI/ML Engineer", case=False, na=False)
        df_filtered = pd.concat([df_filtered, df_patterns[ai_mask]]).drop_duplicates(subset=['Pattern Name'])

    # Sort by priority
    try:
        df_filtered['Priority Score'] = pd.to_numeric(df_filtered['Priority Score'], errors='coerce')
        df_filtered = df_filtered.sort_values(by='Priority Score', ascending=False)
    except Exception:
        pass

    if df_filtered.empty:
        print("\n✅ Your profile seems highly optimized or outside the current database scope. No critical patterns suggested.")
        return

    # Generate the Markdown file
    file_name = "green_recommendations.md"
    with open(file_name, "w", encoding="utf-8") as f:
        f.write(f"# 🌿 Green Coding Recommendations\n\n")
        f.write(f"**Target Roles:** {', '.join(selected_personas)}\n")
        f.write(f"**AI Context:** {'Yes' if is_ai_project else 'No'}\n\n")
        f.write("Based on the Green Software Foundation (GSF) standards, here are the highest priority patterns for your architecture:\n\n")
        
        for _, row in df_filtered.head(7).iterrows():
            f.write(f"### {row['Pattern Name']} (Priority Score: {row.get('Priority Score', 'N/A')})\n")
            f.write(f"- **Lifecycle Phase:** {row.get('SW Lifecycle', 'Unknown')}\n")
            f.write(f"- **Description:** {row.get('Description', 'No description provided.')}\n")
            
            blockers = row.get('Potential Blockers & Problems')
            if pd.notna(blockers) and str(blockers).strip():
                f.write(f"- **Potential Blockers:** {blockers}\n")
                
            gsf_link = row.get('GSF Link')
            if pd.notna(gsf_link) and str(gsf_link).strip():
                f.write(f"- **Official Documentation:** [GSF Pattern Link]({gsf_link})\n")
            f.write("\n---\n\n")

        # --- PRIORITY SCORE EXPLANATION ---
        f.write("### ℹ️ About the Priority Score\n")
        f.write("The **Priority Score** shown above is calculated internally to help surface the most effective patterns first. ")
        f.write("It is derived from the formula: `Priority Score = Feasibility × Impact`.\n\n")
        f.write("- **Feasibility** is ranked on a scale of 1 to 5 based on expected implementation effort.\n")
        f.write("- **Impact** is categorized as High (3 points), Medium (2 points), or Low (1 point).\n\n")
        f.write("> *Note: This score is a baseline recommendation intended to guide your triage process. ")
        f.write("The actual feasibility and impact may vary depending on the specific constraints, scale, and context of your project.*\n")

    print(f"\n✅ Analysis complete! Generated '{file_name}' with your top {len(df_filtered.head(7))} recommended patterns.")
    print("Open it in your IDE to review the architectural suggestions.")

if __name__ == "__main__":
    run_triage()