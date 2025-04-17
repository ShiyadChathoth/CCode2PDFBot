from telegram import ReplyKeyboardMarkup
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackContext,
    ConversationHandler
)
import subprocess
import os
import logging
import asyncio
from asyncio.subprocess import PIPE
import telegram.error
import html
import time
import datetime
import re
import unicodedata
import sys

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Telegram Bot Token
TOKEN = os.getenv('TOKEN')

if not TOKEN:
    raise ValueError("No TOKEN provided in environment variables!")

# States for ConversationHandler
LANGUAGE_CHOICE, CODE, RUNNING, TITLE_INPUT = range(4)

async def start(update: Update, context: CallbackContext) -> int:
    keyboard = [['C', 'Python']]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    
    await update.message.reply_text(
        'Hi! Please select the programming language you want to use:',
        reply_markup=reply_markup
    )
    return LANGUAGE_CHOICE

async def language_choice(update: Update, context: CallbackContext) -> int:
    language = update.message.text
    
    if language not in ['C', 'Python']:
        await update.message.reply_text(
            'Please select either C or Python using the keyboard buttons.'
        )
        return LANGUAGE_CHOICE
    
    context.user_data['language'] = language
    
    await update.message.reply_text(
        f'You selected {language}. Please send me your {language} code, and I will ' +
        ('compile and execute' if language == 'C' else 'execute') + 
        ' it step-by-step.'
    )
    return CODE

def clean_whitespace(code):
    """Clean non-standard whitespace characters from code."""
    cleaned_code = code.replace('\u00A0', ' ')
    for char in code:
        if unicodedata.category(char).startswith('Z') and char != ' ':
            cleaned_code = cleaned_code.replace(char, ' ')
    return cleaned_code

async def handle_code(update: Update, context: CallbackContext) -> int:
    original_code = update.message.text
    code = clean_whitespace(original_code)
    language = context.user_data.get('language', 'C')  # Default to C if not specified
    
    if code != original_code:
        await update.message.reply_text(
            "⚠️ I detected and fixed non-standard whitespace characters in your code that would cause errors."
        )
    
    context.user_data['code'] = code
    context.user_data['output'] = []
    context.user_data['inputs'] = []
    context.user_data['errors'] = []
    context.user_data['waiting_for_input'] = False
    context.user_data['execution_log'] = []
    context.user_data['output_buffer'] = ""
    context.user_data['terminal_log'] = []
    context.user_data['program_completed'] = False
    context.user_data['last_prompt'] = ""
    context.user_data['pending_messages'] = []  # Track messages that need to be sent
    context.user_data['output_complete'] = False  # Flag to track when output is complete
    
    if language == 'C':
        return await handle_c_code(update, context)
    else:  # Python
        return await handle_python_code(update, context)

async def handle_c_code(update: Update, context: CallbackContext) -> int:
    code = context.user_data['code']
    
    # Store the printf statements from the code for later analysis
    printf_patterns = extract_printf_statements(code)
    context.user_data['printf_patterns'] = printf_patterns
    
    logger.info(f"Extracted printf patterns: {printf_patterns}")
    
    try:
        with open("temp.c", "w") as file:
            file.write(code)
        
        compile_result = subprocess.run(["gcc", "temp.c", "-o", "temp"], capture_output=True, text=True)
        
        if compile_result.returncode == 0:
            context.user_data['execution_log'].append({
                'type': 'system',
                'message': 'Code compiled successfully!',
                'timestamp': datetime.datetime.now()
            })
            
            process = await asyncio.create_subprocess_exec(
                "stdbuf", "-o0", "./temp",
                stdin=PIPE,
                stdout=PIPE,
                stderr=PIPE
            )
            
            context.user_data['process'] = process
            await update.message.reply_text("Code compiled successfully! Running now...")
            
            asyncio.create_task(read_process_output(update, context))
            return RUNNING
        else:
            if "stray" in compile_result.stderr and "\\302" in compile_result.stderr:
                code = re.sub(r'[^\x00-\x7F]+', ' ', code)
                
                with open("temp.c", "w") as file:
                    file.write(code)
                
                compile_result = subprocess.run(["gcc", "temp.c", "-o", "temp"], capture_output=True, text=True)
                
                if compile_result.returncode == 0:
                    context.user_data['execution_log'].append({
                        'type': 'system',
                        'message': 'Code compiled successfully after aggressive whitespace cleaning!',
                        'timestamp': datetime.datetime.now()
                    })
                    
                    process = await asyncio.create_subprocess_exec(
                        "stdbuf", "-o0", "./temp",
                        stdin=PIPE,
                        stdout=PIPE,
                        stderr=PIPE
                    )
                    
                    context.user_data['process'] = process
                    await update.message.reply_text("Code compiled successfully after fixing whitespace issues! Running now...")
                    
                    asyncio.create_task(read_process_output(update, context))
                    return RUNNING
            
            context.user_data['execution_log'].append({
                'type': 'error',
                'message': f"Compilation Error:\n{compile_result.stderr}",
                'timestamp': datetime.datetime.now()
            })
            
            if "stray" in compile_result.stderr and ("\\302" in compile_result.stderr or "\\240" in compile_result.stderr):
                await update.message.reply_text(
                    f"Compilation Error (non-standard whitespace characters):\n{compile_result.stderr}\n\n"
                    f"Your code contains invisible non-standard whitespace characters that the compiler cannot process. "
                    f"Try retyping the code in a plain text editor or use a code editor like VS Code."
                )
            else:
                await update.message.reply_text(f"Compilation Error:\n{compile_result.stderr}")
            
            return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"An error occurred: {str(e)}")
        return ConversationHandler.END

async def handle_python_code(update: Update, context: CallbackContext) -> int:
    code = context.user_data['code']
    
    # Store the input statements from the code for later analysis
    input_patterns = extract_input_statements(code)
    context.user_data['input_patterns'] = input_patterns
    
    logger.info(f"Extracted input patterns: {input_patterns}")
    
    try:
        # First, check for syntax errors
        syntax_check = subprocess.run(
            ["python3", "-m", "py_compile"], 
            input=code.encode(), 
            capture_output=True, 
            text=True
        )
        
        if syntax_check.returncode != 0:
            context.user_data['execution_log'].append({
                'type': 'error',
                'message': f"Python Syntax Error:\n{syntax_check.stderr}",
                'timestamp': datetime.datetime.now()
            })
            
            await update.message.reply_text(f"Python Syntax Error:\n{syntax_check.stderr}")
            return ConversationHandler.END
        
        # Create a temporary Python executor file if it doesn't exist
        executor_path = os.path.join(os.getcwd(), "python_executor.py")
        if not os.path.exists(executor_path):
            with open(executor_path, "w") as file:
                file.write("""
import sys
import subprocess
import os
import tempfile

def main():
    # Create a temporary file for the Python code
    with tempfile.NamedTemporaryFile(suffix='.py', delete=False) as temp_file:
        temp_filename = temp_file.name
        
        # Get the Python code from stdin
        python_code = sys.stdin.read()
        
        # Write the code to the temporary file
        temp_file.write(python_code.encode('utf-8'))
    
    try:
        # Execute the Python code in a separate process
        result = subprocess.run(
            ["python3", "-u", temp_filename],
            capture_output=False,
            text=True,
            check=False
        )
        
        # Return the exit code
        sys.exit(result.returncode)
    
    finally:
        # Clean up the temporary file
        if os.path.exists(temp_filename):
            os.remove(temp_filename)

if __name__ == "__main__":
    main()
                """)
        
        # Save the code to a temporary file
        with open("temp.py", "w") as file:
            file.write(code)
        
        context.user_data['execution_log'].append({
            'type': 'system',
            'message': 'Python code validation successful!',
            'timestamp': datetime.datetime.now()
        })
        
        # Run the Python code using our executor script
        # This approach avoids encoding issues by using files instead of direct stdin/stdout
        process = await asyncio.create_subprocess_exec(
            "python3", "-u", executor_path,
            stdin=PIPE,
            stdout=PIPE,
            stderr=PIPE
        )
        
        # Write the code to the executor's stdin
        process.stdin.write(code.encode('utf-8'))
        await process.stdin.drain()
        process.stdin.close()  # Close stdin to signal end of input
        
        context.user_data['process'] = process
        await update.message.reply_text("Python code validation successful! Running now...")
        
        asyncio.create_task(read_process_output(update, context))
        return RUNNING
        
    except Exception as e:
        await update.message.reply_text(f"An error occurred: {str(e)}")
        return ConversationHandler.END

def extract_printf_statements(code):
    """Extract potential printf prompt patterns from C code."""
    # Look for printf statements that might be prompts
    printf_patterns = []
    
    # More comprehensive regex to find printf statements with format specifiers
    # This will match printf("Some text: ") as well as printf("Value %d: ", var)
    pattern = r'printf\s*\(\s*[\"\'](.*?)[\"\'](?:,|\))'
    printf_matches = re.finditer(pattern, code)
    
    for match in printf_matches:
        prompt_text = match.group(1)
        # Clean escape sequences
        prompt_text = prompt_text.replace('\\n', '').replace('\\t', '')
        
        # Replace format specifiers with placeholder
        prompt_text = re.sub(r'%[diouxXfFeEgGaAcspn]', '...', prompt_text)
        
        # Only add non-empty prompts
        if prompt_text.strip():
            printf_patterns.append(prompt_text)
    
    return printf_patterns

def extract_input_statements(code):
    """Extract potential input prompt patterns from Python code."""
    # Look for input statements that might be prompts
    input_patterns = []
    
    # Regex to find input statements with string literals
    # This will match input("Some text: ") patterns
    pattern = r'input\s*\(\s*[\"\'](.*?)[\"\'](?:,|\))'
    input_matches = re.finditer(pattern, code)
    
    for match in input_matches:
        prompt_text = match.group(1)
        # Clean escape sequences
        prompt_text = prompt_text.replace('\\n', '').replace('\\t', '')
        
        # Only add non-empty prompts
        if prompt_text.strip():
            input_patterns.append(prompt_text)
    
    return input_patterns

async def read_process_output(update: Update, context: CallbackContext):
    process = context.user_data['process']
    output = context.user_data['output']
    errors = context.user_data['errors']
    execution_log = context.user_data['execution_log']
    output_buffer = context.user_data['output_buffer']
    terminal_log = context.user_data['terminal_log']
    language = context.user_data.get('language', 'C')
    
    output_seen = False
    read_size = 1024
    timeout_counter = 0
    
    # Add a flag to track if we're currently sending input
    context.user_data['is_sending_input'] = False
    
    while True:
        # If we're currently sending input, wait a bit to ensure proper message order
        if context.user_data.get('is_sending_input', False):
            await asyncio.sleep(0.2)
            continue
            
        stdout_task = asyncio.create_task(process.stdout.read(read_size))
        stderr_task = asyncio.create_task(process.stderr.read(read_size))
        
        done, pending = await asyncio.wait(
            [stdout_task, stderr_task],
            return_when=asyncio.FIRST_COMPLETED,
            timeout=0.3  # Reduced timeout to check more frequently
        )

        if not done:
            for task in pending:
                task.cancel()
            
            if process.returncode is not None:
                if output_buffer:
                    process_output_chunk(context, output_buffer, update)
                    output_buffer = ""
                    context.user_data['output_buffer'] = ""
                
                # If this is the first time we're detecting completion, set up finishing sequence
                if not context.user_data.get('finishing_initiated', False):
                    context.user_data['finishing_initiated'] = True
                    
                    # Wait a bit to ensure all output is processed
                    await asyncio.sleep(1.5)
                    
                    execution_log.append({
                        'type': 'system',
                        'message': 'Program execution completed.',
                        'timestamp': datetime.datetime.now()
                    })
                    
                    context.user_data['program_completed'] = True
                    
                    # Send the completion message and wait for it to be sent
                    await update.message.reply_text("Program execution completed.")
                    
                    # Wait a bit more before asking for title
                    await asyncio.sleep(0.8)
                    
                    await update.message.reply_text("Please provide a title for your program (or type 'skip' to use default):")
                    return TITLE_INPUT
                else:
                    # We're already finishing, just wait
                    await asyncio.sleep(0.1)
                    continue
            
            # If we've seen output and there's been no new output for a while,
            # check if we might be waiting for input
            timeout_counter += 1
            if output_seen and timeout_counter >= 3 and not context.user_data.get('waiting_for_input', False):
                if output_buffer:
                    # Process any remaining output in the buffer
                    # This is crucial for detecting prompts without newlines
                    new_buffer = process_output_chunk(context, output_buffer, update)
                    output_buffer = new_buffer
                    context.user_data['output_buffer'] = new_buffer
                
                # If we're still not waiting for input after processing the buffer,
                # and there's been no output for a while, assume we're waiting
                if not context.user_data.get('waiting_for_input', False) and timeout_counter >= 5:
                    context.user_data['waiting_for_input'] = True
                    
                    # Get the last detected prompt
                    last_prompt = context.user_data.get('last_prompt', "unknown")
                    
                    input_message = f"Program is waiting for input: \"{last_prompt}\"\nPlease provide input (or type 'done' to finish):"
                    
                    execution_log.append({
                        'type': 'system',
                        'message': input_message,
                        'timestamp': datetime.datetime.now()
                    })
                    await update.message.reply_text(input_message)
            
            continue

        # Reset timeout counter when we get output
        timeout_counter = 0
        
        if stdout_task in done:
            stdout_chunk = await stdout_task
            if stdout_chunk:
                decoded_chunk = stdout_chunk.decode()
                output_seen = True
                
                terminal_log.append(decoded_chunk)
                
                output_buffer += decoded_chunk
                context.user_data['output_buffer'] = output_buffer
                
                output_buffer = process_output_chunk(context, output_buffer, update)
                context.user_data['output_buffer'] = output_buffer

        if stderr_task in done:
            stderr_chunk = await stderr_task
            if stderr_chunk:
                decoded_chunk = stderr_chunk.decode()
                
                for line in decoded_chunk.splitlines(True):
                    errors.append(line.strip())
                    
                    execution_log.append({
                        'type': 'error',
                        'message': line.strip(),
                        'timestamp': datetime.datetime.now(),
                        'raw': line
                    })
                    
                    await update.message.reply_text(f"Error: {line.strip()}")

        for task in pending:
            task.cancel()

        # Check if process has completed
        if process.returncode is not None and not context.user_data.get('finishing_initiated', False):
            if output_buffer:
                process_output_chunk(context, output_buffer, update)
                output_buffer = ""
                context.user_data['output_buffer'] = ""
            
            # Mark that we've started the finishing sequence
            context.user_data['finishing_initiated'] = True
            
            # Add a significant delay to ensure all messages have been sent and processed
            await asyncio.sleep(1.5)
            
            execution_log.append({
                'type': 'system',
                'message': 'Program execution completed.',
                'timestamp': datetime.datetime.now()
            })
            
            context.user_data['program_completed'] = True
            
            # Send completion message and wait for it to be sent
            await update.message.reply_text("Program execution completed.")
            
            # Wait a bit more before asking for title
            await asyncio.sleep(0.8)
            
            await update.message.reply_text("Please provide a title for your program (or type 'skip' to use default):")
            return TITLE_INPUT

async def process_output_message(update, message, prefix=""):
    """Helper function to send output messages with better ordering control"""
    try:
        await update.message.reply_text(f"{prefix}{message}")
    except Exception as e:
        logger.error(f"Failed to send message: {e}")

def process_output_chunk(context, buffer, update):
    """Process the output buffer, preserving tabs and whitespace with improved prompt detection."""
    execution_log = context.user_data['execution_log']
    output = context.user_data['output']
    language = context.user_data.get('language', 'C')
    
    # Get the appropriate patterns based on language
    if language == 'C':
        patterns = context.user_data.get('printf_patterns', [])
    else:  # Python
        patterns = context.user_data.get('input_patterns', [])
    
    # First check if the entire buffer might be a prompt without a newline
    if buffer and not buffer.endswith('\n'):
        is_prompt, prompt_text = detect_prompt(buffer, patterns, language)
        if is_prompt:
            context.user_data['last_prompt'] = prompt_text
            
            log_entry = {
                'type': 'prompt',
                'message': buffer,
                'timestamp': datetime.datetime.now(),
                'raw': buffer
            }
            
            execution_log.append(log_entry)
            asyncio.create_task(process_output_message(update, buffer, "Program prompt: "))
            
            # We've processed the buffer as a prompt, so we can be waiting for input
            context.user_data['waiting_for_input'] = True
            return ""
    
    # Normal line-by-line processing for output with newlines
    lines = re.findall(r'[^\n]*\n|[^\n]+$', buffer)
    
    new_buffer = ""
    if lines and not buffer.endswith('\n'):
        new_buffer = lines[-1]
        lines = lines[:-1]
    
    for line in lines:
        line_stripped = line.rstrip()  # Use rstrip() to preserve leading whitespace but remove trailing newlines
        if line_stripped:
            output.append(line_stripped)
            
            # Enhanced prompt detection
            is_prompt, prompt_text = detect_prompt(line_stripped, patterns, language)
            
            if is_prompt:
                context.user_data['last_prompt'] = prompt_text
                
            log_entry = {
                'type': 'prompt' if is_prompt else 'output',
                'message': line_stripped,
                'timestamp': datetime.datetime.now(),
                'raw': line
            }
            
            execution_log.append(log_entry)
            
            prefix = "Program prompt:" if is_prompt else "Program output:"
            asyncio.create_task(process_output_message(update, line_stripped, f"{prefix} "))
    
    return new_buffer

def detect_prompt(line, patterns, language):
    """Enhanced detection for prompts. Returns (is_prompt, prompt_text)"""
    line_text = line.strip()
    
    # First check if the line matches or closely matches any extracted patterns
    for pattern in patterns:
        # Direct match
        if pattern in line_text:
            return True, line_text
        
        # Fuzzy match - check if most of the pattern appears in the line
        # This helps with format specifiers that have been replaced with actual values
        pattern_words = set(re.findall(r'\w+', pattern))
        line_words = set(re.findall(r'\w+', line_text))
        common_words = pattern_words.intersection(line_words)
        
        # If we have a significant match and the pattern ends with a prompt character
        if (len(common_words) >= len(pattern_words) * 0.6 or 
            (len(common_words) > 0 and pattern.rstrip().endswith((':', '?', '>', ' ')))) and len(pattern_words) > 0:
            return True, line_text
    
    # Input keywords check
    input_keywords = ['enter', 'input', 'type', 'provide', 'give', 'value', 'values']
    line_lower = line_text.lower()
    
    for keyword in input_keywords:
        if keyword in line_lower:
            return True, line_text
    
    # Check for ending with prompt characters - added space as a prompt character
    if line_text.rstrip().endswith((':', '?', '>', ' ')):
        return True, line_text
    
    # Check for common patterns where a variable name or description is followed by a colon
    if re.search(r'[A-Za-z0-9_]+\s*:', line_text):
        return True, line_text
    
    # Check for "Enter XXX: " patterns
    if re.search(r'[Ee]nter\s+[^:]+:', line_text):
        return True, line_text
    
    # Additional check for very short outputs that might be prompts
    if len(line_text) < 10 and not line_text.isdigit():
        return True, line_text
    
    # If none of the above, this is probably not a prompt
    return False, ""

async def handle_running(update: Update, context: CallbackContext) -> int:
    user_input = update.message.text
    process = context.user_data.get('process')
    execution_log = context.user_data['execution_log']
    terminal_log = context.user_data['terminal_log']
    language = context.user_data.get('language', 'C')

    # If program is already completed, treat this as title input
    if context.user_data.get('program_completed', False):
        return await handle_title_input(update, context)

    if not process or process.returncode is not None:
        if context.user_data.get('program_completed', False):
            return await handle_title_input(update, context)
        else:
            await update.message.reply_text("Program is not running anymore.")
            return ConversationHandler.END

    if user_input.lower() == 'done':
        execution_log.append({
            'type': 'system',
            'message': 'User terminated the program.',
            'timestamp': datetime.datetime.now()
        })
        
        # Properly close stdin to avoid hanging
        try:
            await process.stdin.drain()
            process.stdin.close()
        except Exception as e:
            logger.error(f"Error closing stdin: {e}")
        
        # For Python programs, we may need to terminate more forcefully
        # since they might be waiting for input in a loop
        if language == 'Python' and process.returncode is None:
            try:
                process.terminate()
                # Give it a moment to terminate gracefully
                await asyncio.sleep(0.5)
                # If still running, force kill
                if process.returncode is None:
                    process.kill()
            except Exception as e:
                logger.error(f"Error terminating process: {e}")
        
        await process.wait()
        
        # Add delay to ensure all output is processed
        await asyncio.sleep(1)
        
        context.user_data['program_completed'] = True
        
        await update.message.reply_text("Please provide a title for your program (or type 'skip' to use default):")
        return TITLE_INPUT
    
    execution_log.append({
        'type': 'input',
        'message': user_input,
        'timestamp': datetime.datetime.now()
    })
    
    terminal_log.append(user_input + "\n")
    
    # Set the flag that we're sending input to prevent output processing during this time
    context.user_data['is_sending_input'] = True
    
    # Send the input confirmation message first and await it to ensure order
    sent_message = await update.message.reply_text(f"Input sent: {user_input}")
    
    # Comprehensive fix for input handling that works with multiple inputs in loops
    try:
        # Ensure the process is still running before sending input
        if process.returncode is not None:
            await update.message.reply_text("Program has ended and cannot receive more input.")
            context.user_data['is_sending_input'] = False
            return RUNNING
            
        # Add newline to input if not already present
        input_with_newline = user_input if user_input.endswith('\n') else user_input + '\n'
        
        # FIX: Properly handle input encoding - check type first before encoding
        input_bytes = None
        if isinstance(input_with_newline, bytes):
            # Already bytes, no need to encode
            input_bytes = input_with_newline
        else:
            # String needs to be encoded
            try:
                input_bytes = input_with_newline.encode('utf-8')
            except UnicodeEncodeError:
                # Try another encoding if utf-8 fails
                input_bytes = input_with_newline.encode('latin-1')
        
        # Write to stdin and flush
        if input_bytes:
            process.stdin.write(input_bytes)
            await process.stdin.drain()
            
            # Store the input for reference
            context.user_data['inputs'].append(user_input)
            context.user_data['waiting_for_input'] = False
        else:
            raise ValueError("Failed to encode input")
            
    except Exception as e:
        logger.error(f"Input handling error: {e}")
        await update.message.reply_text(f"Error sending input: {str(e)}")
        
        # If we encounter a serious error, we might need to restart the process
        if "Broken pipe" in str(e) or "Connection reset" in str(e):
            await update.message.reply_text("Connection to the program was lost. Please restart.")
            return ConversationHandler.END
    
    # Add a small delay to ensure message ordering
    await asyncio.sleep(0.2)
    
    # Reset the flag
    context.user_data['is_sending_input'] = False
    
    return RUNNING

async def handle_title_input(update: Update, context: CallbackContext) -> int:
    title = update.message.text
    language = context.user_data.get('language', 'C')
    
    if title.lower() == 'skip':
        context.user_data['program_title'] = f"{language} Program Execution Report"
    else:
        context.user_data['program_title'] = title
    
    await update.message.reply_text(f"Using title: {context.user_data['program_title']}")
    await generate_and_send_pdf(update, context)
    return ConversationHandler.END

async def generate_and_send_pdf(update: Update, context: CallbackContext):
    try:
        code = context.user_data['code']
        execution_log = context.user_data['execution_log']
        terminal_log = context.user_data['terminal_log']
        language = context.user_data.get('language', 'C')
        program_title = context.user_data.get('program_title', f"{language} Program Execution Report")

        # Generate HTML with proper tab alignment styling and page break control
        html_content = f"""
        <html>
        <head>
            <style>
                @page {{
                    size: A4;
                    margin: 20mm;
                }}
                body {{
                    font-family: Arial, sans-serif;
                    margin: 0;
                    padding: 0;
                }}
                .page {{
                    page-break-after: auto;
                    page-break-inside: avoid;
                }}
                .program-title {{
                    font-size: 30px;
                    font-weight: bold;
                    text-align: center;
                    margin-bottom: 20px;
                    text-decoration: underline;
                    text-decoration-thickness: 5px;
                    border-bottom: 3px;
                }}
                .language-indicator {{
                    font-size: 18px;
                    text-align: center;
                    margin-bottom: 15px;
                    color: #555;
                }}
                pre {{
                    font-family: 'Courier New', monospace;
                    white-space: pre;
                    font-size: 18px;
                    line-height: 1.3;
                    tab-size: 8;
                    -moz-tab-size: 8;
                    -o-tab-size: 8;
                    background: #FFFFFF;
                    padding: 5px;
                    border-radius: 3px;
                    page-break-inside: avoid;
                }}
                .code-section {{
                    page-break-inside: avoid;
                    margin-bottom: 20px;
                }}
                .terminal-view {{
                    margin: 10px 0;
                }}
                .output-title {{
                    font-size: 25px;
                    text-decoration: underline;
                    text-decoration-thickness: 5px;
                    font-weight: bold;
                    margin-top: 20px;
                    page-break-after: avoid;
                }}
                .output-content {{
                    page-break-before: avoid;
                }}
            </style>
        </head>
        <body>
            <div class="page">
                <div class="program-title">{html.escape(program_title)}</div>
                <div class="language-indicator">Language: {language}</div>
                <div class="code-section">
                    <pre><code>{html.escape(code)}</code></pre>
                </div>
                <div class="terminal-view">
                    {reconstruct_terminal_view(context)}
                </div>
            </div>
        </body>
        </html>
        """

        with open("output.html", "w") as file:
            file.write(html_content)

        # Generate sanitized filename from title
        # Replace invalid filename characters with underscores and ensure it ends with .pdf
        sanitized_title = re.sub(r'[\\/*?:"<>|]', "_", program_title)
        sanitized_title = re.sub(r'\s+', "_", sanitized_title)  # Replace spaces with underscores
        pdf_filename = f"{sanitized_title}.pdf"
        
        # Generate PDF with specific options to control page breaks
        subprocess.run([
            "wkhtmltopdf",
            "--enable-smart-shrinking",
            "--print-media-type",
            "--page-size", "A4",
            "output.html", 
            pdf_filename
        ])

        # Send PDF to user
        with open(pdf_filename, 'rb') as pdf_file:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=pdf_file,
                filename=pdf_filename,
                caption=f"Execution report for {program_title}"
            )
            if update.message:
                await update.message.reply_text("You can use /start to run another program or /cancel to end.")
            else:
                await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="You can use /start to run another program or /cancel to end."
            )
    
    except Exception as e:
        await update.message.reply_text(f"Failed to generate PDF: {str(e)}")
    finally:
        await cleanup(context)


def reconstruct_terminal_view(context):
    """Render terminal output with tabs replaced by fixed spaces for PDF compatibility."""
    terminal_log = context.user_data.get('terminal_log', [])

    if terminal_log:
        raw_output = ""
        for line in terminal_log:
            line_with_spaces = line.replace('\t', '        ')  # 8 spaces
            raw_output += line_with_spaces if line_with_spaces.endswith('\n') else line_with_spaces + '\n'

        return f"""
        <div class="terminal-view" style="page-break-inside: avoid;">
            <h1 class="output-title">OUTPUT</h1>
            <div class="output-content" style="
                font-family: 'Courier New', monospace;
                white-space: pre;
                font-size: 18px;
                line-height: 1.2;
                background: #FFFFFF;
                padding: 10px;
                border-radius: 3px;
                overflow-x: auto;
                page-break-inside: avoid;
            ">{html.escape(raw_output)}</div>
        </div>
        """

    return "<pre>No terminal output available</pre>"

async def cleanup(context: CallbackContext):
    process = context.user_data.get('process')
    if process and process.returncode is None:
        process.terminate()
        try:
            await process.wait()
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
    
    # Clean up temporary files based on language
    language = context.user_data.get('language', 'C')
    if language == 'C':
        for file in ["temp.c", "temp", "output.html"]:
            if os.path.exists(file):
                os.remove(file)
    else:  # Python
        for file in ["temp.py", "output.html"]:
            if os.path.exists(file):
                os.remove(file)
    
    # Remove PDF files except the bot files
    for file in os.listdir():
        if file.endswith(".pdf") and file != "bot.py" and file != "dual_language_bot.py":
            os.remove(file)
    
    context.user_data.clear()

async def cancel(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text("Operation cancelled. You can use /start to begin again.")
    return ConversationHandler.END

def main() -> None:
    try:
        application = Application.builder().token(TOKEN).build()
        
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('start', start)],
            states={
                LANGUAGE_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, language_choice)],
                CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_code)],
                RUNNING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_running)],
                TITLE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_title_input)],
            },
            fallbacks=[CommandHandler('cancel', cancel)],
        )
        
        application.add_handler(conv_handler)
        application.add_handler(CommandHandler('cancel', cancel))
        
        logger.info("Bot is about to start polling with token: %s", TOKEN[:10] + "...")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except telegram.error.Conflict as e:
        logger.error(f"Conflict error: {e}. Ensure only one bot instance is running.")
        print("Error: Another instance of this bot is already running. Please stop it and try again.")
        return
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise


if __name__ == '__main__':
    main()
