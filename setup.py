from setuptools import setup, find_packages

setup(
    name="roche_green_sdk",
    version="2.0.1",
    description="Green Software Telemetry SDK for Digital Sustainability",
    author="Green Coding Team",
    # Automatically find the 'roche_green' folder and treat it as a module
    packages=find_packages(),
    
    # Ensure non-Python files (like the GSF Patterns Excel) are bundled during installation
    package_data={
        "roche_green": ["*.xlsx"],
    },
    include_package_data=True,
    
    # Dependencies required for both telemetry (codecarbon) and the suggestion CLI (pandas, openpyxl)
    install_requires=[
        "codecarbon>=2.3.1",
        "requests>=2.28.0",
        "pandas>=1.3.0",
        "openpyxl>=3.0.0"
    ],
    python_requires=">=3.8",
    
    # Expose the CLI tool to the user's terminal globally
    entry_points={
        "console_scripts": [
            "roche-green=roche_green.__main__:main",
        ],
    },
)