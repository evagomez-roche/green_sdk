import sys
from .suggest import run_triage

def main():
    """
    Main entry point for the Roche Green SDK CLI.
    Routes terminal commands to their respective underlying Python modules.
    """
    # Check if the user passed 'suggest' as an argument (e.g., 'roche-green suggest')
    if len(sys.argv) > 1 and sys.argv[1] == "suggest":
        run_triage()
    else:
        # Default help text displayed if no arguments or invalid arguments are provided
        print("🌿 Roche Green SDK v2.0")
        print("Available commands:")
        print("  roche-green suggest  -> Launches the GSF architecture patterns assistant")

if __name__ == "__main__":
    main()