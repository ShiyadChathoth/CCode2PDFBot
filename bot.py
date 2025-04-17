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
import traceback
import signal

# Set up logging with more detailed format
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

# Default timeout settings (in seconds)
DEFAULT_IDLE_TIMEOUT = 60  # Time with no activity before considering idle
DEFAULT_MAX_RUNTIME = 300  # Maximum total runtime regardless of activity (5 minutes)
ACTIVITY_CHECK_INTERVAL = 5  # How often to check for activity

async def start(update: Update, context: CallbackContext) -> int:
    # Clear any previous state
    context.user_data.clear()
    
    keyboard = [['Python']]  # Simplified to only Python for now
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    
    await update.message.reply_text(
        'Hi! I can execute Python code and generate a PDF report of the execution.',
        reply_markup=reply_markup
    )
    return LANGUAGE_CHOICE

async def language_choice(update: Update, context: CallbackContext) -> int:
    language = update.message.text
    
    if language != 'Python':
        await update.message.reply_text('Currently only Python is supported. Please select Python.')
        return LANGUAGE_CHOICE
    
    context.user_data['language'] = language
    
    await update.message.reply_text(
        'You selected Python. Please send me your Python code, and I will execute it.'
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
    context.user_data['title_requested'] = False  # Flag to track if title has been requested
    
    # Initialize activity monitoring
    context.user_data['activity_stats'] = {
        'start_time': time.time(),
        'last_output_time': time.time(),
        'last_activity_check': time.time(),
        'output_count': 0,
        'idle_time': 0,
        'warnings_sent': 0,
        'is_active': True
    }
    
    # Check for syntax errors
    with open("temp.py", "w") as file:
        file.write(code)
    
    syntax_check = subprocess.run(
        ["python3", "-m", "py_compile", "temp.py"], 
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
    
    context.user_data['execution_log'].append({
        'type': 'system',
        'message': 'Python code validation successful!',
        'timestamp': datetime.datetime.now()
    })
    
    # Extract input patterns for later use
    input_patterns = extract_input_statements(code)
    context.user_data['input_patterns'] = input_patterns
    logger.info(f"Extracted input patterns: {input_patterns}")
    
    await update.message.reply_text("Python code validation successful! Ready to execute.")
    
    # Use a simpler, more direct approach for execution
    return await execute_python_directly(update, context)

def extract_input_statements(code):
    """Extract potential input prompt patterns from Python code."""
    input_patterns = []
    pattern = r'input\s*\(\s*[\"\'](.*?)[\"\'](?:,|\))'
    input_matches = re.finditer(pattern, code)
    
    for match in input_matches:
        prompt_text = match.group(1)
        prompt_text = prompt_text.replace('\\n', '').replace('\\t', '')
        if prompt_text.strip():
            input_patterns.append(prompt_text)
    
    return input_patterns

async def execute_python_directly(update: Update, context: CallbackContext) -> int:
    """Execute Python code directly with a simpler approach"""
    try:
        # Get the user's code
        code = context.user_data['code']
        
        # Create a simple wrapper script that just runs the code
        with open("simple_execution.py", "w") as file:
            file.write(code)
        
        # Log that we're about to execute
        logger.info("Executing Python code directly")
        
        # Start the process with unbuffered output
        process = await asyncio.create_subprocess_exec(
            "python3", "-u", "simple_execution.py",
            stdin=PIPE,
            stdout=PIPE,
            stderr=PIPE
        )
        
        # Store the process
        context.user_data['process'] = process
        context.user_data['process_pid'] = process.pid
        
        # Start reading output
        asyncio.create_task(read_process_output(update, context))
        
        # Start the activity monitor
        asyncio.create_task(monitor_process_activity(update, context))
        
        # Return to RUNNING state to handle input
        return RUNNING
        
    except Exception as e:
        logger.error(f"Error in execute_python_directly: {str(e)}\n{traceback.format_exc()}")
        await update.message.reply_text(f"An error occurred during execution: {str(e)}")
        return ConversationHandler.END

async def monitor_process_activity(update: Update, context: CallbackContext):
    """Monitor process activity to detect if it's stuck or making progress"""
    try:
        process = context.user_data.get('process')
        stats = context.user_data.get('activity_stats', {})
        
        while process and process.returncode is None:
            # Wait for the check interval
            await asyncio.sleep(ACTIVITY_CHECK_INTERVAL)
            
            # If process has completed, exit the monitor
            if process.returncode is not None or context.user_data.get('program_completed', False):
                logger.info("Process completed, stopping activity monitor")
                return
            
            current_time = time.time()
            stats['last_activity_check'] = current_time
            
            # Calculate time since last output
            time_since_output = current_time - stats.get('last_output_time', current_time)
            
            # Check if we're waiting for input - if so, we're not idle
            if context.user_data.get('waiting_for_input', False):
                logger.info("Program is waiting for input, resetting idle time")
                stats['idle_time'] = 0
                continue
            
            # If we haven't seen output in a while, increment idle time
            if time_since_output > ACTIVITY_CHECK_INTERVAL:
                stats['idle_time'] += ACTIVITY_CHECK_INTERVAL
                logger.info(f"No recent output, idle time: {stats['idle_time']} seconds")
                
                # Check if we've been idle too long
                if stats['idle_time'] >= DEFAULT_IDLE_TIMEOUT:
                    # If we haven't sent many warnings yet, send one and extend timeout
                    if stats['warnings_sent'] < 2:
                        stats['warnings_sent'] += 1
                        await update.message.reply_text(
                            f"⚠️ Your program has been idle for {stats['idle_time']} seconds. "
                            f"It will be terminated soon if no activity is detected."
                        )
                        # Give it some more time after the warning
                        stats['idle_time'] = DEFAULT_IDLE_TIMEOUT * 0.7
                    else:
                        # We've sent enough warnings, terminate the process
                        logger.warning(f"Process idle timeout after {stats['idle_time']} seconds")
                        await update.message.reply_text(
                            f"Program execution terminated after {stats['idle_time']} seconds of inactivity."
                        )
                        
                        try:
                            process.terminate()
                            await asyncio.sleep(0.5)
                            if process.returncode is None:
                                process.kill()
                        except Exception as e:
                            logger.error(f"Error terminating idle process: {e}")
                        
                        # Mark program as completed
                        context.user_data['program_completed'] = True
                        
                        # Ask for title only if not already requested
                        if not context.user_data.get('title_requested', False):
                            context.user_data['title_requested'] = True
                            await update.message.reply_text("Please provide a title for your program (or type 'skip' to use default):")
                        
                        return TITLE_INPUT
            
            # Check total runtime
            total_runtime = current_time - stats.get('start_time', current_time)
            if total_runtime >= DEFAULT_MAX_RUNTIME:
                logger.warning(f"Process max runtime exceeded: {total_runtime} seconds")
                await update.message.reply_text(
                    f"Program execution terminated after reaching the maximum runtime of {DEFAULT_MAX_RUNTIME} seconds."
                )
                
                try:
                    process.terminate()
                    await asyncio.sleep(0.5)
                    if process.returncode is None:
                        process.kill()
                except Exception as e:
                    logger.error(f"Error terminating long-running process: {e}")
                
                # Mark program as completed
                context.user_data['program_completed'] = True
                
                # Ask for title only if not already requested
                if not context.user_data.get('title_requested', False):
                    context.user_data['title_requested'] = True
                    await update.message.reply_text("Please provide a title for your program (or type 'skip' to use default):")
                
                return TITLE_INPUT
            
            # Update the stats in context
            context.user_data['activity_stats'] = stats
    
    except Exception as e:
        logger.error(f"Error in monitor_process_activity: {str(e)}\n{traceback.format_exc()}")

async def read_process_output(update: Update, context: CallbackContext):
    process = context.user_data['process']
    output = context.user_data['output']
    errors = context.user_data['errors']
    execution_log = context.user_data['execution_log']
    output_buffer = context.user_data['output_buffer']
    terminal_log = context.user_data['terminal_log']
    stats = context.user_data.get('activity_stats', {})
    
    output_seen = False
    read_size = 1024
    
    # Add a flag to track if we're currently sending input
    context.user_data['is_sending_input'] = False
    
    try:
        while True:
            # If we're currently sending input, wait a bit to ensure proper message order
            if context.user_data.get('is_sending_input', False):
                await asyncio.sleep(0.2)
                continue
                
            # Check if process has completed
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
                    
                    # Ask for title only if not already requested
                    if not context.user_data.get('title_requested', False):
                        context.user_data['title_requested'] = True
                        # Wait a bit more before asking for title
                        await asyncio.sleep(0.8)
                        await update.message.reply_text("Please provide a title for your program (or type 'skip' to use default):")
                    
                    return TITLE_INPUT
                else:
                    # We're already finishing, just wait
                    await asyncio.sleep(0.1)
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
                
                # If no output and we've seen output before, check if we might be waiting for input
                if output_seen and output_buffer:
                    # Process any remaining output in the buffer
                    # This is crucial for detecting prompts without newlines
                    new_buffer = process_output_chunk(context, output_buffer, update)
                    output_buffer = new_buffer
                    context.user_data['output_buffer'] = new_buffer
                
                await asyncio.sleep(0.1)
                continue

            # Reset idle time when we get output
            if done:
                stats['last_output_time'] = time.time()
                stats['idle_time'] = 0
                context.user_data['activity_stats'] = stats
            
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
                    
                    # Update activity stats
                    stats['output_count'] += 1
                    stats['last_output_time'] = time.time()
                    stats['idle_time'] = 0
                    context.user_data['activity_stats'] = stats

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
                    
                    # Update activity stats - errors are also a form of output
                    stats['output_count'] += 1
                    stats['last_output_time'] = time.time()
                    stats['idle_time'] = 0
                    context.user_data['activity_stats'] = stats

            for task in pending:
                task.cancel()
    
    except Exception as e:
        logger.error(f"Error in read_process_output: {str(e)}\n{traceback.format_exc()}")
        try:
            await update.message.reply_text(f"Error monitoring program output: {str(e)}")
            
            # Ask for title only if not already requested
            if not context.user_data.get('title_requested', False):
                context.user_data['title_requested'] = True
                await update.message.reply_text("Please provide a title for your program (or type 'skip' to use default):")
            
            context.user_data['program_completed'] = True
            
            return TITLE_INPUT
        except Exception as inner_e:
            logger.error(f"Error sending error message: {inner_e}")
            return RUNNING

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
    patterns = context.user_data.get('input_patterns', [])
    stats = context.user_data.get('activity_stats', {})
    
    # First check if the entire buffer might be a prompt without a newline
    if buffer and not buffer.endswith('\n'):
        is_prompt, prompt_text = detect_prompt(buffer, patterns)
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
            
            # Update activity stats
            stats['last_output_time'] = time.time()
            stats['idle_time'] = 0
            context.user_data['activity_stats'] = stats
            
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
            is_prompt, prompt_text = detect_prompt(line_stripped, patterns)
            
            if is_prompt:
                context.user_data['last_prompt'] = prompt_text
                context.user_data['waiting_for_input'] = True
            
            log_entry = {
                'type': 'prompt' if is_prompt else 'output',
                'message': line_stripped,
                'timestamp': datetime.datetime.now(),
                'raw': line
            }
            
            execution_log.append(log_entry)
            
            prefix = "Program prompt:" if is_prompt else "Program output:"
            asyncio.create_task(process_output_message(update, line_stripped, f"{prefix} "))
            
            # Update activity stats
            stats['last_output_time'] = time.time()
            stats['idle_time'] = 0
            context.user_data['activity_stats'] = stats
    
    return new_buffer

def detect_prompt(line, patterns):
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
    
    # If program is already completed, treat this as title input
    if context.user_data.get('program_completed', False):
        return await handle_title_input(update, context)
    
    # Check if user wants to terminate
    if user_input.lower() == 'done':
        process = context.user_data.get('process')
        if process and process.returncode is None:
            try:
                process.terminate()
                await asyncio.sleep(0.5)
                if process.returncode is None:
                    process.kill()
            except Exception as e:
                logger.error(f"Error terminating process: {e}")
        
        await update.message.reply_text("Program execution terminated by user.")
        context.user_data['program_completed'] = True
        
        # Ask for title only if not already requested
        if not context.user_data.get('title_requested', False):
            context.user_data['title_requested'] = True
            await update.message.reply_text("Please provide a title for your program (or type 'skip' to use default):")
        
        return TITLE_INPUT
    
    # Send input to the process
    process = context.user_data.get('process')
    if not process or process.returncode is not None:
        await update.message.reply_text("Program is not running anymore.")
        return ConversationHandler.END
    
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
            
            # Update activity stats
            stats = context.user_data.get('activity_stats', {})
            stats['last_output_time'] = time.time()  # Input counts as activity
            stats['idle_time'] = 0
            context.user_data['activity_stats'] = stats
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
    
    # Mark that we've received the title input
    context.user_data['title_received'] = True
    
    if title.lower() == 'skip':
        context.user_data['program_title'] = "Python Program Execution Report"
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
        program_title = context.user_data.get('program_title', "Python Program Execution Report")

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
                <div class="language-indicator">Language: Python</div>
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
        
        # Check if wkhtmltopdf is installed
        try:
            wkhtmltopdf_check = subprocess.run(["which", "wkhtmltopdf"], capture_output=True, text=True)
            if wkhtmltopdf_check.returncode != 0:
                # Install wkhtmltopdf if not found
                logger.info("wkhtmltopdf not found, installing...")
                install_result = subprocess.run(["apt-get", "update", "-y"], capture_output=True, text=True)
                install_result = subprocess.run(["apt-get", "install", "-y", "wkhtmltopdf"], capture_output=True, text=True)
                if install_result.returncode != 0:
                    logger.error(f"Failed to install wkhtmltopdf: {install_result.stderr}")
                    raise Exception("Failed to install PDF generation tool")
        except Exception as e:
            logger.error(f"Error checking/installing wkhtmltopdf: {str(e)}")
            # Continue anyway, it might still work
        
        # Generate PDF with specific options to control page breaks
        try:
            pdf_result = subprocess.run([
                "wkhtmltopdf",
                "--enable-smart-shrinking",
                "--print-media-type",
                "--page-size", "A4",
                "output.html", 
                pdf_filename
            ], capture_output=True, text=True)
            
            if pdf_result.returncode != 0:
                logger.error(f"PDF generation error: {pdf_result.stderr}")
                raise Exception(f"Failed to generate PDF: {pdf_result.stderr}")
            
            logger.info(f"PDF generated successfully: {pdf_filename}")
        except Exception as e:
            logger.error(f"Error generating PDF: {str(e)}")
            await update.message.reply_text(f"Error generating PDF: {str(e)}")
            return

        # Send PDF to user
        try:
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
            logger.error(f"Error sending PDF: {str(e)}")
            await update.message.reply_text(f"Error sending PDF: {str(e)}")
    
    except Exception as e:
        logger.error(f"Failed to generate PDF: {str(e)}\n{traceback.format_exc()}")
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
        try:
            process.terminate()
            # Give it a moment to terminate gracefully
            await asyncio.sleep(0.5)
            # If still running, force kill
            if process.returncode is None:
                process.kill()
        except Exception as e:
            logger.error(f"Error terminating process during cleanup: {e}")
        
        try:
            await process.wait()
        except asyncio.TimeoutError:
            logger.error("Timeout waiting for process to terminate during cleanup")
    
    # Clean up temporary files
    for file in ["temp.py", "direct_execution.py", "simple_execution.py", "output.html"]:
        if os.path.exists(file):
            try:
                os.remove(file)
            except Exception as e:
                logger.error(f"Error removing file {file}: {str(e)}")
    
    # Remove PDF files except the bot files
    for file in os.listdir():
        if file.endswith(".pdf") and file != "bot.py" and file != "no_psutil_bot.py":
            try:
                os.remove(file)
            except Exception as e:
                logger.error(f"Error removing PDF file {file}: {str(e)}")
    
    # Clear user data but preserve certain flags for conversation flow
    title_requested = context.user_data.get('title_requested', False)
    title_received = context.user_data.get('title_received', False)
    program_completed = context.user_data.get('program_completed', False)
    
    context.user_data.clear()
    
    # Restore critical flags if needed for conversation flow
    if not title_received and (title_requested or program_completed):
        context.user_data['title_requested'] = title_requested
        context.user_data['program_completed'] = program_completed

async def cancel(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text("Operation cancelled. You can use /start to begin again.")
    await cleanup(context)
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
    except Exception as e:
        logger.error(f"Unexpected error: {e}\n{traceback.format_exc()}")
        raise


if __name__ == '__main__':
    main()
