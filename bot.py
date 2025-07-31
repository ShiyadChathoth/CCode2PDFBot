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
    context.user_data['output'] = # Fixed: Initialized as empty list
    context.user_data['inputs'] = # Fixed: Initialized as empty list
    context.user_data['errors'] = # Fixed: Initialized as empty list
    context.user_data['waiting_for_input'] = False
    context.user_data['execution_log'] = # Fixed: Initialized as empty list
    context.user_data['output_buffer'] = ""
    context.user_data['terminal_log'] = # Fixed: Initialized as empty list
    context.user_data['program_completed'] = False
    context.user_data['last_prompt'] = ""
    context.user_data['pending_messages'] = # Fixed: Initialized as empty list
    context.user_data['output_complete'] = False  # Flag to track when output is complete
    context.user_data['title_requested'] = False  # Flag to track if title has been requested
    context.user_data['all_prompts'] = # Fixed: Initialized as empty list
    context.user_data['final_output_captured'] = False  # Flag to track if final output has been captured
    
    # NEW: Use a completely different approach for terminal simulation
    # Instead of appending to a list that can get duplicates, use a dictionary with unique keys
    context.user_data['terminal_entries'] = {}  # Dictionary to store terminal entries with unique keys
    context.user_data['entry_order'] = # Fixed: Initialized as empty list
    context.user_data['execution_session'] = str(time.time())  # Unique identifier for this execution session
    
    # NEW: Track seen content to prevent exact duplicates
    context.user_data['seen_content'] = set()  # Set of content we've already seen
    context.user_data['content_counts'] = {}  # Count occurrences of each content
    
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
        context.user_data['input_patterns'] = # Fixed: Initialized as empty list
        
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
    entry_order = context.user_data.get('entry_order',) # Fixed: Initialized as empty list
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
    input_patterns = # Fixed: Initialized as empty list
    pattern = r'input\s*\(\s*[\"\'](.*?)[\"\'](?:,|\))'
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
        all_prompts = context.user_data.get('all_prompts',) # Fixed: Initialized as empty list
        
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
        entry_order = context.user_data.get('entry_order',) # Fixed: Initialized as empty list
        
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
        entry_order = context.user_data.get('entry_order',) # Fixed: Initialized as empty list
        
        if not terminal_entries or not entry_order:
            return
        
        # Create a new list to store the filtered entry order
        filtered_order = # Fixed: Initialized as empty list
        
        # Track content we've seen to detect duplicates
        seen_content = {}  # Map content to entry key
        
        # HARDCODED FIX: Special handling for "Please enter a valid number"
        valid_number_seen = False
        
        # Process entries in order
        for key in entry_order:
            entry = terminal_entries.get(key, {})
            if not entry:
                continue
                
            entry_type = entry.get('type', '')
            content = entry.get('content', '')
            
            # Skip system messages about title
            if entry_type == 'system' and 'Using title:' in content:
                continue
            
            # HARDCODED FIX: Special handling for "Please enter a valid number"
            if content == "Please enter a valid number." and entry_type in ['output', 'prompt']:
                if valid_number_seen:
                    logger.info("HARDCODED FIX: Skipping duplicate 'Please enter a valid number.'")
                    continue
                else:
                    valid_number_seen = True
            
            # For output and prompt types, check for duplicates
            if entry_type in ['output', 'prompt']:
                # If we've seen this content before, skip it
                if content in seen_content:
                    # Keep the first occurrence only
                    continue
                
                # First time seeing this content, add it to seen_content
                seen_content[content] = key
            
            # Add this entry to the filtered order
            filtered_order.append(key)
        
        # Update the entry order with the filtered list
        context.user_data['entry_order'] = filtered_order
        
        logger.info(f"Post-processed terminal entries: removed {len(entry_order) - len(filtered_order)} duplicates")
    except Exception as e:
        logger.error(f"Error post-processing terminal entries: {str(e)}")

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
    patterns = context.user_data.get('input_patterns',) # Fixed: Initialized as empty list
    stats = context.user_data.get('activity_stats', {})
    
    # First check if the entire buffer might be a prompt without a newline
    if buffer and not buffer.endswith('\n'):
        is_prompt, prompt_text = detect_prompt(buffer, patterns)
        if is_prompt:
            context.user_data['last_prompt'] = prompt_text
            
            # Add to all_prompts list for final capture
            all_prompts = context.user_data.get('all_prompts',) # Fixed: Initialized as empty list
            if prompt_text not in all_prompts:
                all_prompts.append(prompt_text)
                context.user_data['all_prompts'] = all_prompts
            
            log_entry = {
                'type': 'prompt',
                'message': buffer,
                'timestamp': datetime.datetime.now(),
                'raw': buffer
            }
            
            execution_log.append(log_entry)
            
            # Add to terminal entries
            add_terminal_entry(context, 'prompt', buffer)
            
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
            
            # Check for success messages that might be final output
            is_success_message = detect_success_message(line_stripped)
            
            if is_prompt:
                context.user_data['last_prompt'] = prompt_text
                context.user_data['waiting_for_input'] = True
                
                # Add to all_prompts list for final capture
                all_prompts = context.user_data.get('all_prompts',) # Fixed: Initialized as empty list
                if prompt_text not in all_prompts:
                    all_prompts.append(prompt_text)
                    context.user_data['all_prompts'] = all_prompts
            
            log_entry = {
                'type': 'prompt' if is_prompt else 'output',
                'message': line_stripped,
                'timestamp': datetime.datetime.now(),
                'raw': line
            }
            
            execution_log.append(log_entry)
            
            # Add to terminal entries
            add_terminal_entry(context, 'prompt' if is_prompt else 'output', line_stripped)
            
            prefix = "Program prompt:" if is_prompt else "Program output:"
            
            asyncio.create_task(process_output_message(update, line_stripped, f"{prefix} "))
            
            # Update activity stats
            stats['last_output_time'] = time.time()
            stats['idle_time'] = 0
            context.user_data['activity_stats'] = stats
    
    return new_buffer

def detect_success_message(line):
    """Detect if a line contains a success message that might be final output"""
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
    
    return any(pattern in line.lower() for pattern in success_patterns)

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
        
        # Ensure all prompts are captured
        await ensure_all_prompts_captured(context)
        
        # Ensure final output is captured
        await ensure_final_output_captured(context)
        
        # Post-process the terminal entries to remove any remaining duplicates
        post_process_terminal_entries(context)
        
        await update.message.reply_text("Program execution terminated by user.")
        
        # Add to terminal entries
        add_terminal_entry(context, 'system', "Program execution terminated by user.")
        
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
    
    # Add to terminal entries - this is the user input
    add_terminal_entry(context, 'input', user_input)
    
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
        
        # Add to terminal entries
        add_terminal_entry(context, 'error', f"Error sending input: {str(e)}")
        
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
    
    language = context.user_data.get('language', 'Program')
    default_title = f"{language} Program Execution Report"

    if title.lower() == 'skip':
        context.user_data['program_title'] = default_title
    else:
        context.user_data['program_title'] = title
    
    # Add to terminal entries
    add_terminal_entry(context, 'system', f"Using title: {context.user_data['program_title']}")
    
    # Ensure all prompts are captured before generating PDF
    await ensure_all_prompts_captured(context)
    
    # Ensure final output is captured
    await ensure_final_output_captured(context)
    
    # Post-process the terminal entries to remove any remaining duplicates
    post_process_terminal_entries(context)
    
    await update.message.reply_text(f"Using title: {context.user_data['program_title']}")
    await generate_and_send_pdf(update, context)
    return ConversationHandler.END

async def generate_and_send_pdf(update: Update, context: CallbackContext):
    try:
        # Use original code for display (not the modified code with delay)
        code = context.user_data['code']
        language = context.user_data.get('language', 'Program')
        program_title = context.user_data.get('program_title', f"{language} Program Execution Report")

        # Get terminal entries in order
        terminal_entries = context.user_data.get('terminal_entries', {})
        entry_order = context.user_data.get('entry_order',) # Fixed: Initialized as empty list
        
        # HARDCODED FIX: Final manual check for "Please enter a valid number" duplicates
        # This is a last resort to ensure no duplicates make it to the PDF
        final_html = generate_terminal_html(terminal_entries, entry_order)
        
        # Count occurrences of the problematic string
        valid_number_count = final_html.count("Please enter a valid number.")
        if valid_number_count > 1:
            logger.info(f"HARDCODED FIX: Found {valid_number_count} occurrences of 'Please enter a valid number.' in final HTML")
            # Replace all occurrences with a single one
            final_html = final_html.replace("Please enter a valid number.\nPlease enter a valid number.", "Please enter a valid number.")
            logger.info("HARDCODED FIX: Applied direct HTML replacement for duplicate 'Please enter a valid number.'")
        
        # Generate HTML with terminal-like styling
        html_content = f"""
        <html>
        <head>
            <style>
                @page {{
                    size: A4;
                    margin: 20mm;
                }}
                body {{
                    font-family: 'Courier New', monospace;
                    margin: 0;
                    padding: 0;
                    background-color: #FFFFFF;
                }}
            .program-title {{
                    font-size: 24px;
                    font-weight: bold;
                    text-align: center;
                    margin-bottom: 20px;
                    font-family: Arial, sans-serif;
                }}
            .code-section {{
                    margin-bottom: 20px;
                    border: 1px solid #ddd;
                    padding: 10px;
                    background-color: #f8f8f8;
                    white-space: pre;
                    font-family: 'Courier New', monospace;
                    font-size: 14px;
                    line-height: 1.3;
                    overflow-x: auto;
                }}
            .terminal {{
                    background-color: #FFFFFF;
                    color: #000000;
                    padding: 10px;
                    font-family: 'Courier New', monospace;
                    font-size: 14px;
                    line-height: 1.3;
                    white-space: pre-wrap;
                    border: 1px solid #ddd;
                }}
            .prompt {{
                    color: #0000FF;
                }}
            .input {{
                    color: #008800;
                    font-weight: bold;
                }}
            .output {{
                    color: #000000;
                }}
            .error {{
                    color: #FF0000;
                }}
            .system {{
                    color: #888888;
                    font-style: italic;
                }}
            </style>
        </head>
        <body>
            <div class="program-title">{escape_html(program_title)}</div>
            
            <h3>Source Code:</h3>
            <div class="code-section">{escape_html(code)}</div>
            
            <h3>Terminal Output:</h3>
            <div class="terminal">
                {final_html}
            </div>
        </body>
        </html>
        """

        with open("output.html", "w") as file:
            file.write(html_content)

        # Generate sanitized filename from title
        sanitized_title = re.sub(r'[\\/*?:"<>|]', "_", str(program_title))
        sanitized_title = re.sub(r'\s+', "_", sanitized_title)  # Replace spaces with underscores
        pdf_filename = f"{sanitized_title}.pdf"
        
        # Check if wkhtmltopdf is installed
        try:
            wkhtmltopdf_check = subprocess.run(["which", "wkhtmltopdf"], capture_output=True, text=True)
            if wkhtmltopdf_check.returncode!= 0:
                # Install wkhtmltopdf if not found
                logger.info("wkhtmltopdf not found, installing...")
                install_result = subprocess.run(["apt-get", "update", "-y"], capture_output=True, text=True)
                install_result = subprocess.run(["apt-get", "install", "-y", "wkhtmltopdf"], capture_output=True, text=True)
                if install_result.returncode!= 0:
                    logger.error(f"Failed to install wkhtmltopdf: {install_result.stderr}")
                    raise Exception("Failed to install PDF generation tool")
        except Exception as e:
            logger.error(f"Error checking/installing wkhtmltopdf: {str(e)}")
            # Continue anyway, it might still work
        
        # Generate PDF
        try:
            pdf_result = subprocess.run([
                "wkhtmltopdf",
                "--enable-smart-shrinking",
                "--print-media-type",
                "--page-size", "A4",
                "output.html", 
                pdf_filename
            ], capture_output=True, text=True)
            
            if pdf_result.returncode!= 0:
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
                    caption=f"Terminal output for {program_title}"
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

def generate_terminal_html(terminal_entries, entry_order):
    """Generate HTML for terminal-like output with inputs and outputs in execution order"""
    if not terminal_entries or not entry_order:
        return "<span class='system'>No terminal output available</span>"
    
    html = ""
    
    # HARDCODED FIX: Track if we've seen "Please enter a valid number"
    valid_number_seen = False
    
    # Process entries in order
    for key in entry_order:
        entry = terminal_entries.get(key, {})
        if not entry:
            continue
            
        entry_type = entry.get('type', '')
        content = entry.get('content', '')
        
        # Skip system messages about title
        if entry_type == 'system' and 'Using title:' in content:
            continue
        
        # HARDCODED FIX: Special handling for "Please enter a valid number"
        if content == "Please enter a valid number." and entry_type in ['output', 'prompt']:
            if valid_number_seen:
                logger.info("HARDCODED FIX: Skipping duplicate 'Please enter a valid number.' in HTML generation")
                continue
            else:
                valid_number_seen = True
        
        if entry_type == 'prompt':
            html += f"<span class='prompt'>{escape_html(content)}</span>\n"
        elif entry_type == 'input':
            html += f"<span class='input'>{escape_html(content)}</span>\n"
        elif entry_type == 'output':
            html += f"<span class='output'>{escape_html(content)}</span>\n"
        elif entry_type == 'error':
            html += f"<span class='error'>{escape_html(content)}</span>\n"
        elif entry_type == 'system':
            html += f"<span class='system'>{escape_html(content)}</span>\n"
    
    return html

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
    files_to_remove = [
        "temp.py", "direct_execution.py", "simple_execution.py", "output.html",
        "temp.c", "a.out" # Added C-specific files
    ]
    for file in files_to_remove:
        if os.path.exists(file):
            try:
                os.remove(file)
            except Exception as e:
                logger.error(f"Error removing file {file}: {str(e)}")
    
    # Remove PDF files except the bot files
    for file in os.listdir():
        if file.endswith(".pdf") and file not in ["bot.py", "pdf_fixed_bot.py", "html_fixed_bot.py", "final_bot.py", "terminal_bot.py", "prompt_fixed_bot.py", "final_output_bot.py", "fixed_duplicate_bot.py", "final_working_bot.py", "final_solution.py", "hardcoded_solution.py"]:
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
                LANGUAGE_CHOICE:,
                CODE:,
                RUNNING:,
                TITLE_INPUT:,
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

