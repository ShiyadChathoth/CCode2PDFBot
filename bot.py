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
import time
import datetime
import re
import unicodedata
import sys
import traceback
import signal
import json

# Custom HTML escape function that doesn't rely on the html module
def escape_html(text):
    """Custom HTML escape function that works with any input type"""
    if text is None:
        return ""
    text = str(text)  # Convert to string first
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")

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
FINAL_OUTPUT_CAPTURE_DELAY = 2  # Delay after program completion to ensure final output is captured

async def start(update: Update, context: CallbackContext) -> int:
    # Clear any previous state
    context.user_data.clear()
    
    keyboard = [['Python'], ['C']]  # Added 'C' option
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    
    await update.message.reply_text(
        'Hi! I can execute Python or C code and generate a PDF report of the execution.',
        reply_markup=reply_markup
    )
    return LANGUAGE_CHOICE

async def language_choice(update: Update, context: CallbackContext) -> int:
    language = update.message.text
    
    if language not in ['Python', 'C']: # Allow both Python and C
        await update.message.reply_text('Currently only Python and C are supported. Please select one.')
        return LANGUAGE_CHOICE
    
    context.user_data['language'] = language
    
    await update.message.reply_text(
        f'You selected {language}. Please send me your {language} code, and I will execute it.'
    )
    return CODE

def clean_whitespace(code):
    """Clean non-standard whitespace characters from code."""
    cleaned_code = code.replace('\u00A0', ' ')
    for char in code:
        if unicodedata.category(char).startswith('Z') and char!= ' ':
            cleaned_code = cleaned_code.replace(char, ' ')
    return cleaned_code

async def handle_code(update: Update, context: CallbackContext) -> int:
    original_code = update.message.text
    code = clean_whitespace(original_code)
    language = context.user_data['language'] # Get selected language
    
    if code!= original_code:
        await update.message.reply_text(
            "⚠️ I detected and fixed non-standard whitespace characters in your code that would cause errors."
        )
    
    context.user_data['code'] = code  # Store original code for display
    context.user_data['output'] = []
    context.user_data['inputs'] = []
    context.user_data['errors'] = []
    context.user_data['waiting_for_input'] = False
    context.user_data['execution_log'] = []
    context.user_data['output_buffer'] = ""
    context.user_data['terminal_log'] = []
    context.user_data['program_completed'] = False
    context.user_data['last_prompt'] = ""
    context.user_data['pending_messages'] = []
    context.user_data['output_complete'] = False  # Flag to track when output is complete
    context.user_data['title_requested'] = False  # Flag to track if title has been requested
    context.user_data['all_prompts'] = []
    context.user_data['final_output_captured'] = False  # Flag to track if final output has been captured
    
    # NEW: Use a completely different approach for terminal simulation
    # Instead of appending to a list that can get duplicates, use a dictionary with unique keys
    context.user_data['terminal_entries'] = {}
    context.user_data['entry_order'] = []
    context.user_data['execution_session'] = str(time.time())  # Unique identifier for this execution session
    
    # NEW: Track seen content to prevent exact duplicates
    context.user_data['seen_content'] = set()
    context.user_data['content_counts'] = {}
    
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
    
    if language == 'Python':
        # Modify the code to add a delay after completion to ensure final output is captured
        modified_code = add_output_capture_delay(code)
        context.user_data['modified_code'] = modified_code  # Store modified code for execution
        
        # Check for syntax errors
        with open("temp.py", "w") as file:
            file.write(code)  # Use original code for syntax check
        
        syntax_check = subprocess.run(
            ["python3", "-m", "py_compile", "temp.py"], 
            capture_output=True, 
            text=True
        )
        
        if syntax_check.returncode!= 0:
            context.user_data['execution_log'].append({
                'type': 'error',
                'message': f"Python Syntax Error:\n{syntax_check.stderr}",
                'timestamp': datetime.datetime.now()
            })
            
            # Add to terminal entries
            add_terminal_entry(context, 'system', f"Python Syntax Error:\n{syntax_check.stderr}")
            
            await update.message.reply_text(f"Python Syntax Error:\n{syntax_check.stderr}")
            return ConversationHandler.END
        
        context.user_data['execution_log'].append({
            'type': 'system',
            'message': 'Python code validation successful!',
            'timestamp': datetime.datetime.now()
        })
        
        # Add to terminal entries
        add_terminal_entry(context, 'system', 'Python code validation successful! Ready to execute.')
        
        # Extract input patterns for later use (Python-specific)
        input_patterns = extract_input_statements(code)
        context.user_data['input_patterns'] = input_patterns
        logger.info(f"Extracted input patterns: {input_patterns}")
        
        await update.message.reply_text("Python code validation successful! Ready to execute.")
        
    elif language == 'C':
        # For C, no modified_code needed, original code is compiled
        context.user_data['modified_code'] = code 
        
        # Save C code to a temporary.c file
        with open("temp.c", "w") as file:
            file.write(code)
        
        # Compile C code using gcc
        compile_result = subprocess.run(
            ["gcc", "temp.c", "-o", "a.out"], 
            capture_output=True, 
            text=True
        )
        
        if compile_result.returncode!= 0:
            # Compilation failed
            error_message = f"C Compilation Error:\n{compile_result.stderr}"
            context.user_data['execution_log'].append({
                'type': 'error',
                'message': error_message,
                'timestamp': datetime.datetime.now()
            })
            
            # Add to terminal entries
            add_terminal_entry(context, 'error', error_message)
            
            await update.message.reply_text(error_message)
            return ConversationHandler.END
        
        context.user_data['execution_log'].append({
            'type': 'system',
            'message': 'C code compilation successful! Ready to execute.',
            'timestamp': datetime.datetime.now()
        })
        
        # Add to terminal entries
        add_terminal_entry(context, 'system', 'C code compilation successful! Ready to execute.')
        
        # For C, input patterns are not extracted in the same way as Python
        context.user_data['input_patterns'] = []
        
        await update.message.reply_text("C code compilation successful! Ready to execute.")
    
    # Use a simpler, more direct approach for execution
    return await execute_program_directly(update, context)

def add_terminal_entry(context, entry_type, content, sequence=None):
    """
    Add an entry to terminal entries with guaranteed uniqueness.
    Uses a combination of entry type, content, and sequence number to create a unique key.
    Also checks for exact duplicate content to prevent repeating the same output.
    """
    # HARDCODED FIX: Special handling for "Please enter a valid number"
    if content == "Please enter a valid number." and entry_type in ['output', 'prompt']:
        # Check if we've already seen this exact content
        if "Please enter a valid number." in context.user_data.get('seen_content', set()):
            logger.info("HARDCODED FIX: Skipping duplicate 'Please enter a valid number.'")
            return False
    
    # Get the current execution session
    session = context.user_data.get('execution_session', str(time.time()))
    
    # If no sequence is provided, use the current timestamp as a sequence
    if sequence is None:
        sequence = f"{time.time():.6f}"
    
    # Create a unique key for this entry
    entry_key = f"{session}:{entry_type}:{sequence}"
    
    # Check if this key already exists
    if entry_key in context.user_data.get('terminal_entries', {}):
        # If it exists, don't add it again
        return False
    
    # NEW: Check for exact duplicate content
    # Only apply this check to output and prompt types, not system messages or inputs
    if entry_type in ['output', 'prompt']:
        # Get the set of seen content
        seen_content = context.user_data.get('seen_content', set())
        content_counts = context.user_data.get('content_counts', {})
        
        # Check if we've seen this exact content before
        if content in seen_content:
            # We've seen this content before, check how many times
            count = content_counts.get(content, 0)
            
            # If we've seen it more than once, don't add it again
            if count >= 1:
                logger.info(f"Skipping exact duplicate content: {content[:50]}...")
                return False
            
            # Increment the count
            content_counts[content] = count + 1
            context.user_data['content_counts'] = content_counts
        else:
            # First time seeing this content, add it to the set
            seen_content.add(content)
            content_counts[content] = 1
            context.user_data['seen_content'] = seen_content
            context.user_data['content_counts'] = content_counts
    
    # Add to terminal entries dictionary
    terminal_entries = context.user_data.get('terminal_entries', {})
    terminal_entries[entry_key] = {
        'type': entry_type,
        'content': content,
        'timestamp': time.time()
    }
    context.user_data['terminal_entries'] = terminal_entries
    
    # Add to entry order list to maintain order
    entry_order = context.user_data.get('entry_order', [])
    entry_order.append(entry_key)
    context.user_data['entry_order'] = entry_order
    
    return True

def add_output_capture_delay(code):
    """Add a delay after program completion to ensure final output is captured (Python-specific)"""
    # Add import for time if not already present
    if "import time" not in code and "from time import" not in code:
        code_lines = code.split('\n')
        # Find a good place to add the import (after other imports)
        import_added = False
        for i, line in enumerate(code_lines):
            if line.startswith('import ') or line.startswith('from '):
                # Add after the last import
                import_index = i
                import_added = True
        
        if import_added:
            code_lines.insert(import_index + 1, 'import time  # Added for output capture')
        else:
            # No imports found, add at the beginning
            code_lines.insert(0, 'import time  # Added for output capture')
        
        # Add a delay at the end of the code
        code_lines.append('\n# Added delay to ensure final output is captured')
        code_lines.append(f'time.sleep({FINAL_OUTPUT_CAPTURE_DELAY})  # Ensure final output is captured')
        
        return '\n'.join(code_lines)
    else:
        # Time is already imported, just add the delay at the end
        return code + f'\n\n# Added delay to ensure final output is captured\ntime.sleep({FINAL_OUTPUT_CAPTURE_DELAY})  # Ensure final output is captured'

def extract_input_statements(code):
    """Extract potential input prompt patterns from Python code."""
    input_patterns = []
    pattern = r'input\s*\(\s*[\"\"](.*?)[\"\"](?:,|\))'
    input_matches = re.finditer(pattern, code)
    
    for match in input_matches:
        prompt_text = match.group(1)
        prompt_text = prompt_text.replace('\\n', '').replace('\\t', '')
        if prompt_text.strip():
            input_patterns.append(prompt_text)
    
    return input_patterns

async def execute_program_directly(update: Update, context: CallbackContext) -> int:
    """Execute program (Python or C) directly with a simpler approach"""
    try:
        language = context.user_data['language']
        
        if language == 'Python':
            # Get the modified code with output capture delay
            modified_code = context.user_data['modified_code']
            
            # Create a simple wrapper script that just runs the code
            with open("simple_execution.py", "w") as file:
                file.write(modified_code)
            
            command = ["python3", "-u", "simple_execution.py"]
            logger.info("Executing Python code directly with output capture delay")
            
        elif language == 'C':
            # For C, we execute the compiled binary 'a.out'
            command = ["./a.out"]
            logger.info("Executing compiled C code:./a.out")
            
        # Start the process with unbuffered output
        process = await asyncio.create_subprocess_exec(
            *command, # Use *command to unpack the list
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
        logger.error(f"Error in execute_program_directly: {str(e)}\n{traceback.format_exc()}")
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
                        
                        # Add to terminal entries
                        add_terminal_entry(
                            context, 
                            'system', 
                            f"⚠️ Program idle for {stats['idle_time']} seconds. Will terminate soon if no activity."
                        )
                        
                        # Give it some more time after the warning
                        stats['idle_time'] = DEFAULT_IDLE_TIMEOUT * 0.7
                    else:
                        # We've sent enough warnings, terminate the process
                        logger.warning(f"Process idle timeout after {stats['idle_time']} seconds")
                        
                        # Check if there's a last prompt that needs to be captured
                        await ensure_all_prompts_captured(context)
                        
                        # Ensure final output is captured
                        await ensure_final_output_captured(context)
                        
                        await update.message.reply_text(
                            f"Program execution terminated after {stats['idle_time']} seconds of inactivity."
                        )
                        
                        # Add to terminal entries
                        add_terminal_entry(
                            context, 
                            'system', 
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
                
                # Check if there's a last prompt that needs to be captured
                await ensure_all_prompts_captured(context)
                
                # Ensure final output is captured
                await ensure_final_output_captured(context)
                
                await update.message.reply_text(
                    f"Program execution terminated after reaching the maximum runtime of {DEFAULT_MAX_RUNTIME} seconds."
                )
                
                # Add to terminal entries
                add_terminal_entry(
                    context, 
                    'system', 
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

async def ensure_all_prompts_captured(context):
    """Ensure all detected prompts are captured in the terminal entries"""
    try:
        # Get all prompts
        all_prompts = context.user_data.get('all_prompts', [])
        
        # Add any missing prompts to the terminal entries
        for i, prompt in enumerate(all_prompts):
            # Use the index as part of the sequence to maintain order
            add_terminal_entry(context, 'prompt', prompt, f"prompt_{i}")
        
        # Check if there's a last prompt that needs to be captured
        last_prompt = context.user_data.get('last_prompt', '')
        if last_prompt and last_prompt not in all_prompts:
            add_terminal_entry(context, 'prompt', last_prompt, f"last_prompt")
            
    except Exception as e:
        logger.error(f"Error ensuring all prompts are captured: {str(e)}")

async def ensure_final_output_captured(context):
    """Ensure final output messages are captured in the terminal entries"""
    try:
        # Check if we've already done this
        if context.user_data.get('final_output_captured', False):
            return
            
        # Mark that we've done this check
        context.user_data['final_output_captured'] = True
        
        # Get the output buffer and process any remaining content
        output_buffer = context.user_data.get('output_buffer', '')
        if output_buffer:
            logger.info(f"Processing final output buffer: {output_buffer}")
            
            # Add to terminal entries as output
            add_terminal_entry(context, 'output', output_buffer, f"final_buffer")
            
            # Add to execution log
            context.user_data['execution_log'].append({
                'type': 'output',
                'message': output_buffer,
                'timestamp': datetime.datetime.now(),
                'raw': output_buffer
            })
            
            # Clear the buffer
            context.user_data['output_buffer'] = ''
            
        # Look for success patterns
        success_patterns = [
            "correct",
            "congratulations",
            "success",
            "completed",
            "finished",
            "won",
            "🎉",
            "✓",
            "✅"
        ]
        
        # Get all terminal entries
        terminal_entries = context.user_data.get('terminal_entries', {})
        entry_order = context.user_data.get('entry_order', [])
        
        # Check the last few entries for success messages that might have been missed
        checked_entries = 0
        for i in range(min(10, len(entry_order))):
            if checked_entries >= 5:
                break
                
            entry_key = entry_order[-(i+1)]
            entry = terminal_entries.get(entry_key, {})
            
            if entry.get('type') in ['output', 'prompt']:
                checked_entries += 1
                content = entry.get('content', '').lower()
                
                # Check if this looks like a success message that wasn't properly captured
                if any(pattern in content.lower() for pattern in success_patterns):
                    logger.info(f"Found potential success message: {content}")
                    
                    # If it's not already marked as output, add it again as output to ensure it's displayed
                    if entry.get('type')!= 'output':
                        add_terminal_entry(context, 'output', entry.get('content', ''), f"success_message_{i}")
    except Exception as e:
        logger.error(f"Error ensuring final output is captured: {str(e)}")

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
                    
                    # Wait a bit longer to ensure all output is processed
                    # This is especially important for capturing the final success message
                    await asyncio.sleep(2.0)
                    
                    # Ensure all prompts are captured
                    await ensure_all_prompts_captured(context)
                    
                    # Ensure final output is captured
                    await ensure_final_output_captured(context)
                    
                    # NEW: Post-process the terminal entries to remove any remaining duplicates
                    post_process_terminal_entries(context)
                    
                    execution_log.append({
                        'type': 'system',
                        'message': 'Program execution completed.',
                        'timestamp': datetime.datetime.now()
                    })
                    
                    # Add to terminal entries
                    add_terminal_entry(context, 'system', 'Program execution completed.', 'completion')
                    
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
                        
                        # Add to terminal entries
                        add_terminal_entry(context, 'error', line.strip())
                        
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
            
            # Add to terminal entries
            add_terminal_entry(context, 'system', f"Error monitoring program output: {str(e)}")
            
            # Ask for title only if not already requested
            if not context.user_data.get('title_requested', False):
                context.user_data['title_requested'] = True
                await update.message.reply_text("Please provide a title for your program (or type 'skip' to use default):")
            
            context.user_data['program_completed'] = True
            
            return TITLE_INPUT
        except Exception as inner_e:
            logger.error(f"Error sending error message: {inner_e}")
            return RUNNING

def post_process_terminal_entries(context):
    """
    Post-process terminal entries to remove any remaining duplicates.
    This is a final pass to ensure no duplicates make it to the PDF.
    """
    try:
        # Get all terminal entries and their order
        terminal_entries = context.user_data.get('terminal_entries', {})
        entry_order = context.user_data.get('entry_order', [])
        
        if not terminal_entries or not entry_order:
            return
        
        # Create a new list for the cleaned order
        cleaned_entry_order = []
        # Create a set to keep track of seen content (for exact duplicates)
        seen_content_for_post_processing = set()
        
        for entry_key in entry_order:
            entry = terminal_entries.get(entry_key)
            if not entry:
                continue
            
            content = entry.get('content')
            entry_type = entry.get('type')
            
            # Only consider output and prompt types for content de-duplication
            if entry_type in ['output', 'prompt']:
                if content in seen_content_for_post_processing:
                    # This is a duplicate, skip it
                    logger.info(f"Post-processing: Skipping duplicate content: {content[:50]}...")
                    continue
                else:
                    seen_content_for_post_processing.add(content)
            
            cleaned_entry_order.append(entry_key)
            
        context.user_data['entry_order'] = cleaned_entry_order
        
    except Exception as e:
        logger.error(f"Error during post-processing terminal entries: {str(e)}")

def process_output_chunk(context, current_buffer, update):
    """
    Processes a chunk of output, looking for prompts and handling full lines.
    Returns the remaining buffer.
    """
    execution_log = context.user_data['execution_log']
    output = context.user_data['output']
    
    # Split by newline, but keep the newline character if it exists
    lines = current_buffer.splitlines(keepends=True)
    new_buffer = ""
    
    for line in lines:
        if line.endswith('\n'):
            # It's a complete line, process it
            processed_line = line.strip('\n')
            
            # Check if it's a prompt (ends with common prompt characters)
            is_prompt = False
            if processed_line.endswith((':', '?', '>')) or any(p in processed_line for p in context.user_data.get('input_patterns', [])):
                is_prompt = True
                context.user_data['waiting_for_input'] = True
                context.user_data['last_prompt'] = processed_line
                context.user_data['all_prompts'].append(processed_line)
                
                execution_log.append({
                    'type': 'prompt',
                    'message': processed_line,
                    'timestamp': datetime.datetime.now(),
                    'raw': line
                })
                
                # Add to terminal entries
                add_terminal_entry(context, 'prompt', processed_line)
                
                # Send prompt to user
                asyncio.create_task(update.message.reply_text(f"Program asks for input: {processed_line}"))
                
            else:
                # It's regular output
                output.append(processed_line)
                execution_log.append({
                    'type': 'output',
                    'message': processed_line,
                    'timestamp': datetime.datetime.now(),
                    'raw': line
                })
                
                # Add to terminal entries
                add_terminal_entry(context, 'output', processed_line)
                
                # Send output to user if it's not a prompt
                asyncio.create_task(update.message.reply_text(f"Program output: {processed_line}"))
                
            context.user_data['output_buffer'] = "" # Clear buffer after processing a full line
            
        else:
            # It's an incomplete line, keep it in the buffer
            new_buffer += line
            
            # Check if the incomplete line is a prompt (without a newline)
            is_prompt = False
            if new_buffer.endswith((':', '?', '>')) or any(p in new_buffer for p in context.user_data.get('input_patterns', [])):
                is_prompt = True
                context.user_data['waiting_for_input'] = True
                context.user_data['last_prompt'] = new_buffer
                context.user_data['all_prompts'].append(new_buffer)
                
                execution_log.append({
                    'type': 'prompt',
                    'message': new_buffer,
                    'timestamp': datetime.datetime.now(),
                    'raw': new_buffer # Use new_buffer as raw for incomplete prompt
                })
                
                # Add to terminal entries
                add_terminal_entry(context, 'prompt', new_buffer)
                
                # Send prompt to user
                asyncio.create_task(update.message.reply_text(f"Program asks for input: {new_buffer}"))
                
                # Clear the buffer as we've processed this as a prompt
                new_buffer = ""
                context.user_data['output_buffer'] = ""
                
    return new_buffer

async def handle_input(update: Update, context: CallbackContext) -> int:
    user_input = update.message.text
    process = context.user_data.get('process')
    
    if not process or process.returncode is not None:
        await update.message.reply_text("No program is currently running or it has already finished.")
        return ConversationHandler.END
    
    context.user_data['inputs'].append(user_input)
    context.user_data['waiting_for_input'] = False
    context.user_data['is_sending_input'] = True # Set flag when sending input
    
    context.user_data['execution_log'].append({
        'type': 'input',
        'message': user_input,
        'timestamp': datetime.datetime.now()
    })
    
    # Add to terminal entries
    add_terminal_entry(context, 'input', user_input)
    
    try:
        # Write input to the process's stdin, followed by a newline
        process.stdin.write((user_input + '\n').encode())
        await process.stdin.drain()
        logger.info(f"Sent input: {user_input}")
        
        # Reset the flag after sending input
        context.user_data['is_sending_input'] = False
        
        # Reset idle time after receiving input
        stats = context.user_data.get('activity_stats', {})
        stats['last_output_time'] = time.time() # Treat input as activity
        stats['idle_time'] = 0
        context.user_data['activity_stats'] = stats
        
        return RUNNING
    except Exception as e:
        logger.error(f"Error writing to process stdin: {str(e)}\n{traceback.format_exc()}")
        await update.message.reply_text(f"Failed to send input to program: {str(e)}")
        
        # Reset the flag in case of error
        context.user_data['is_sending_input'] = False
        
        return ConversationHandler.END

async def generate_pdf_report(update: Update, context: CallbackContext, title: str):
    try:
        # Ensure all prompts are captured before generating PDF
        await ensure_all_prompts_captured(context)
        
        # Ensure final output is captured before generating PDF
        await ensure_final_output_captured(context)
        
        # Post-process terminal entries one last time
        post_process_terminal_entries(context)
        
        # Create a temporary HTML file for the report
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Program Execution Report</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; background-color: #f4f4f4; color: #333; }}
                .container {{ background-color: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
                h1 {{ color: #0056b3; border-bottom: 2px solid #0056b3; padding-bottom: 10px; margin-bottom: 20px; }}
                h2 {{ color: #0056b3; margin-top: 30px; border-bottom: 1px solid #eee; padding-bottom: 5px; }}
                pre {{ background-color: #eee; padding: 15px; border-radius: 5px; overflow-x: auto; white-space: pre-wrap; word-wrap: break-word; }}
                .code-section pre {{ background-color: #272822; color: #f8f8f2; padding: 15px; border-radius: 5px; overflow-x: auto; white-space: pre-wrap; word-wrap: break-word; }}
                .log-entry {{ margin-bottom: 10px; padding: 8px; border-radius: 4px; }}
                .log-entry.system {{ background-color: #e0f7fa; border-left: 5px solid #00bcd4; }}
                .log-entry.output {{ background-color: #e8f5e9; border-left: 5px solid #4caf50; }}
                .log-entry.input {{ background-color: #fff3e0; border-left: 5px solid #ff9800; }}
                .log-entry.prompt {{ background-color: #e3f2fd; border-left: 5px solid #2196f3; }}
                .log-entry.error {{ background-color: #ffebee; border-left: 5px solid #f44336; }}
                .timestamp {{ font-size: 0.8em; color: #777; margin-right: 10px; }}
                .terminal-view {{ background-color: #000; color: #0f0; padding: 15px; border-radius: 5px; overflow-x: auto; white-space: pre-wrap; word-wrap: break-word; font-family: 'Courier New', Courier, monospace; line-height: 1.2; }}
                .terminal-view .prompt {{ color: #0ff; }}
                .terminal-view .input {{ color: #ff0; }}
                .terminal-view .error {{ color: #f00; }}
                .terminal-view .system {{ color: #fff; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>Program Execution Report</h1>
                <p><strong>Title:</strong> {escape_html(title)}</p>
                <p><strong>Date:</strong> {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
                <p><strong>Language:</strong> {escape_html(context.user_data.get('language', 'N/A'))}</p>

                <h2>Original Code</h2>
                <div class="code-section"><pre>{escape_html(context.user_data.get('code', 'No code provided'))}</pre></div>

                <h2>Terminal View</h2>
                <div class="terminal-view">
        """
        
        # Sort terminal entries by timestamp to ensure chronological order
        sorted_entry_keys = sorted(context.user_data.get('entry_order', []), 
                                   key=lambda k: context.user_data['terminal_entries'][k]['timestamp'])
        
        for entry_key in sorted_entry_keys:
            entry = context.user_data['terminal_entries'][entry_key]
            entry_type = entry['type']
            content = entry['content']
            
            # Add a small space before each line for better readability in terminal view
            formatted_content = " " + escape_html(content)
            
            if entry_type == 'prompt':
                html_content += f"<span class=\"prompt\">{formatted_content}</span>\n"
            elif entry_type == 'input':
                html_content += f"<span class=\"input\">{formatted_content}</span>\n"
            elif entry_type == 'error':
                html_content += f"<span class=\"error\">{formatted_content}</span>\n"
            elif entry_type == 'system':
                html_content += f"<span class=\"system\">{formatted_content}</span>\n"
            else: # output
                html_content += f"{formatted_content}\n"
                
        html_content += f"""
                </div>

                <h2>Execution Log</h2>
        """
        for entry in context.user_data['execution_log']:
            timestamp = entry['timestamp'].strftime('%H:%M:%S.%f')[:-3] # Milliseconds
            html_content += f"""
                <div class="log-entry {entry['type']}">
                    <span class="timestamp">{timestamp}</span>
                    <strong>{entry['type'].capitalize()}:</strong> <pre>{escape_html(entry['message'])}</pre>
                </div>
            """
        
        html_content += f"""
            </div>
        </body>
        </html>
        """
        
        with open("report.html", "w") as f:
            f.write(html_content)
        
        # Convert HTML to PDF using weasyprint
        # This requires weasyprint to be installed and wkhtmltopdf (or similar) to be available
        # For simplicity, we'll use a direct command if weasyprint is not directly callable
        # A more robust solution would involve a Python library like xhtml2pdf or ReportLab
        
        # Using xhtml2pdf for direct Python conversion
        from xhtml2pdf import pisa
        
        with open("report.pdf", "wb") as pdf_file:
            pisa_status = pisa.CreatePDF(
                html_content,                # the HTML to convert
                dest=pdf_file)               # file handle to receive result
        
        if pisa_status.err:
            logger.error(f"PDF generation error: {pisa_status.err}")
            await update.message.reply_text("Failed to generate PDF report.")
        else:
            await update.message.reply_document(document=open("report.pdf", 'rb'))
            logger.info("PDF report sent.")
            
    except Exception as e:
        logger.error(f"Error generating PDF report: {str(e)}\n{traceback.format_exc()}")
        await update.message.reply_text(f"An error occurred while generating the PDF report: {str(e)}")

async def handle_title(update: Update, context: CallbackContext) -> int:
    title = update.message.text
    if title.lower() == 'skip':
        title = "Program Execution Report"
    
    await generate_pdf_report(update, context, title)
    
    # Clean up temporary files
    for f in ["temp.py", "temp.c", "a.out", "simple_execution.py", "report.html", "report.pdf"]:
        if os.path.exists(f):
            os.remove(f)
            
    context.user_data.clear() # Clear all user data after report generation
    return ConversationHandler.END

async def cancel(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text('Operation cancelled.')
    
    # Clean up temporary files
    for f in ["temp.py", "temp.c", "a.out", "simple_execution.py", "report.html", "report.pdf"]:
        if os.path.exists(f):
            os.remove(f)
            
    context.user_data.clear() # Clear all user data on cancel
    return ConversationHandler.END

def main() -> None:
    """Start the bot."""
    # Create the Application and pass your bot's token.
    application = Application.builder().token(TOKEN).build()

    # Add conversation handler with states
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            LANGUAGE_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, language_choice)],
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_code)],
            RUNNING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input)],
            TITLE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_title)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    application.add_handler(conv_handler)

    # Run the bot until the user presses Ctrl-C
    logger.info("Bot started. Press Ctrl-C to stop.")
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except telegram.error.TelegramError as e:
        logger.error(f"Telegram error: {e}")
    except Exception as e:
        logger.critical(f"Critical error in bot: {e}\n{traceback.format_exc()}")
    finally:
        logger.info("Bot stopped.")

if __name__ == '__main__':
    main()

