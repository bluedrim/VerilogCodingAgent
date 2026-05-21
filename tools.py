import os
from langchain_core.tools import tool

@tool
def read_file_content(file_path: str) -> str:
    """Reads the content of a specified file. Returns content or an error message."""
    try:
        with open(file_path, 'r') as f:
            content = f.read()
        return content
    except FileNotFoundError:
        return f"ERROR: File not found at {file_path}"
    except Exception as e:
        return f"ERROR: An unexpected error occurred - {e}"

@tool
def write_verilog_file(filename: str, content: str) -> str:
    """Writes the given content to a Verilog file specified by the filename."""
    try:
        if not isinstance(filename, str) or not filename.strip():
            return "Error writing to file: invalid filename"

        # Limit output to a local folder and Verilog-related extensions only.
        raw_filename = filename.strip()
        safe_filename = os.path.basename(raw_filename)
        if raw_filename != safe_filename:
            return f"Error writing to file: path segments are not allowed ({filename})"

        allowed_exts = {".v", ".sv", ".vh", ".svh"}
        _, ext = os.path.splitext(safe_filename)
        if ext.lower() not in allowed_exts:
            return f"Error writing to file: unsupported extension '{ext}'"

        output_dir = "generated_rtl"
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, safe_filename)

        with open(output_path, "w") as f:
            f.write(content)
        return f"Successfully wrote Verilog code to {output_path}"
    except Exception as e:
        return f"Error writing to file: {e}"

@tool
def ask_human_for_feedback(generated_code: str) -> str:
    """
    Presents the generated Verilog code to the human for review and
    asks for their feedback. Returns the human's response.
    """
    print("\n" + "-"*20 + " VERIFICATION " + "-"*20)
    print("The following Verilog code has been generated:")
    print("\n" + generated_code)
    print("\n" + "-"*56)
    feedback = input("Is this correct? (approve / reject / <your feedback>): ")
    return feedback
