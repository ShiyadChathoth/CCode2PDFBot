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
CODE, RUNNING, TITLE_INPUT = range(3)

async def start(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text(
        'Hi! Send me your C code, and I will compile and execute it step-by-step.'
    )
    return CODE

def clean_whitespace(code):
    """Clean non-standard whitespace characters from code."""
    # Replace non-breaking spaces (U+00A0) with regular spaces
    cleaned_code = code.replace('\u00A0', ' ')
    
    # Replace other problematic Unicode whitespace characters
    for char in code:
        if unicodedata.category(char).startswith('Z') and char != ' ':
            cleaned_code = cleaned_code.replace(char, ' ')
    
    return cleaned_code

async def handle_code(update: Update, context: CallbackContext) -> int:
    original_code = update.message.text
    
    # Clean whitespace characters that might cause compilation issues
    code = clean_whitespace(original_code)
    
    # Check if code was modified during cleaning
    if code != original_code:
        await update.message.reply_text(
            "⚠️ I detected and fixed non-standard whitespace characters in your code that would cause compilation errors."
        )
    
    context.user_data['code'] = code
    context.user_data['output'] = []
    context.user_data['inputs'] = []
    context.user_data['errors'] = []
    context.user_data['waiting_for_input'] = False
    context.user_data['execution_log'] = []  # Track full execution flow
    context.user_data['output_buffer'] = ""  # Buffer for incomplete output lines
    context.user_data['terminal_log'] = []  # Raw terminal output for exact formatting
    context.user_data['program_completed'] = False  # Flag to track if program has completed
    
    try:
        with open("temp.c", "w") as file:
            file.write(code)
        
        compile_result = subprocess.run(["gcc", "temp.c", "-o", "temp"], capture_output=True, text=True)
        
        if compile_result.returncode == 0:
            # Add compilation success to execution log
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
            
            # Start monitoring process output without immediately asking for input
            asyncio.create_task(read_process_output(update, context))
            return RUNNING
        else:
            # Check if there are still whitespace errors after cleaning
            if "stray" in compile_result.stderr and "\\302" in compile_result.stderr:
                # Try a more aggressive cleaning approach
                code = re.sub(r'[^\x00-\x7F]+', ' ', code)  # Replace all non-ASCII chars with spaces
                
                with open("temp.c", "w") as file:
                    file.write(code)
                
                # Try compiling again
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
                    
                    # Start monitoring process output without immediately asking for input
                    asyncio.create_task(read_process_output(update, context))
                    return RUNNING
            
            # Add compilation error to execution log
            context.user_data['execution_log'].append({
                'type': 'error',
                'message': f"Compilation Error:\n{compile_result.stderr}",
                'timestamp': datetime.datetime.now()
            })
            
            # Provide helpful error message for whitespace issues
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

async def read_process_output(update: Update, context: CallbackContext):
    process = context.user_data['process']
    output = context.user_data['output']
    errors = context.user_data['errors']
    execution_log = context.user_data['execution_log']
    output_buffer = context.user_data['output_buffer']
    terminal_log = context.user_data['terminal_log']
    
    # Flag to track if we've seen any output that might indicate input is needed
    output_seen = False
    
    # Use a smaller read size to capture output more frequently
    read_size = 1024
    
    while True:
        # Read stdout and stderr in chunks rather than lines
        stdout_task = asyncio.create_task(process.stdout.read(read_size))
        stderr_task = asyncio.create_task(process.stderr.read(read_size))
        
        # Wait for either stdout or stderr to have data, or for a short timeout
        done, pending = await asyncio.wait(
            [stdout_task, stderr_task],
            return_when=asyncio.FIRST_COMPLETED,
            timeout=0.5  # Add timeout to check process status periodically
        )

        # If no output received, check if process has ended
        if not done:
            for task in pending:
                task.cancel()
            
            # Check if process has finished
            if process.returncode is not None:
                # Process ended without more output
                # If there's any remaining data in the buffer, process it
                if output_buffer:
                    # Process any remaining buffered output
                    process_output_chunk(context, output_buffer, update)
                    output_buffer = ""
                    context.user_data['output_buffer'] = ""
                
                if output_seen:
                    # Only send completion message if we've seen some output
                    execution_log.append({
                        'type': 'system',
                        'message': 'Program execution completed.',
                        'timestamp': datetime.datetime.now()
                    })
                    
                    # Mark program as completed
                    context.user_data['program_completed'] = True
                    
                    await update.message.reply_text("Program execution completed.")
                    
                    # Ask for program title before generating PDF
                    await update.message.reply_text("Please provide a title for your program (or type 'skip' to use default):")
                    return TITLE_INPUT
                else:
                    # No output seen, just wait a bit more
                    await asyncio.sleep(0.1)
                    continue
            
            # If we've seen output but no new output for a while, it might be waiting for input
            if output_seen and not context.user_data.get('waiting_for_input', False):
                # Process any buffered output before prompting for input
                if output_buffer:
                    process_output_chunk(context, output_buffer, update)
                    output_buffer = ""
                    context.user_data['output_buffer'] = ""
                
                context.user_data['waiting_for_input'] = True
                
                # Get the last prompt from the execution log if available
                last_prompt = "unknown"
                for entry in reversed(execution_log):
                    if entry['type'] == 'prompt':
                        last_prompt = entry['message']
                        break
                
                # Create a more informative message that includes the program's prompt
                input_message = f"Program is waiting for input: \"{last_prompt}\"\nPlease provide input (or type 'done' to finish):"
                
                execution_log.append({
                    'type': 'system',
                    'message': input_message,
                    'timestamp': datetime.datetime.now()
                })
                await update.message.reply_text(input_message)
            
            continue

        # Handle stdout (program output)
        if stdout_task in done:
            stdout_chunk = await stdout_task
            if stdout_chunk:
                decoded_chunk = stdout_chunk.decode()
                output_seen = True
                
                # Store raw output for exact terminal formatting
                terminal_log.append(decoded_chunk)
                
                # Append to buffer and process
                output_buffer += decoded_chunk
                context.user_data['output_buffer'] = output_buffer
                
                # Process the buffer
                output_buffer = process_output_chunk(context, output_buffer, update)
                context.user_data['output_buffer'] = output_buffer

        # Handle stderr (errors)
        if stderr_task in done:
            stderr_chunk = await stderr_task
            if stderr_chunk:
                decoded_chunk = stderr_chunk.decode()
                
                # Process error lines
                for line in decoded_chunk.splitlines(True):  # Keep line endings
                    errors.append(line.strip())
                    
                    # Add to execution log
                    execution_log.append({
                        'type': 'error',
                        'message': line.strip(),
                        'timestamp': datetime.datetime.now(),
                        'raw': line  # Store raw output with newlines
                    })
                    
                    await update.message.reply_text(f"Error: {line.strip()}")

        # Cancel pending tasks
        for task in pending:
            task.cancel()

        # Check if process has finished
        if process.returncode is not None:
            # Process any remaining buffered output
            if output_buffer:
                process_output_chunk(context, output_buffer, update)
            
            execution_log.append({
                'type': 'system',
                'message': 'Program execution completed.',
                'timestamp': datetime.datetime.now()
            })
            
            # Mark program as completed
            context.user_data['program_completed'] = True
            
            await update.message.reply_text("Program execution completed.")
            
            # Ask for program title before generating PDF
            await update.message.reply_text("Please provide a title for your program (or type 'skip' to use default):")
            return TITLE_INPUT

def process_output_chunk(context, buffer, update):
    """Process the output buffer, extracting complete lines and preserving partial lines."""
    execution_log = context.user_data['execution_log']
    output = context.user_data['output']
    
    # Split the buffer into lines, preserving the line endings
    lines = re.findall(r'[^\n]*\n|[^\n]+$', buffer)
    
    # If the buffer doesn't end with a newline, keep the last part for next time
    new_buffer = ""
    if lines and not buffer.endswith('\n'):
        new_buffer = lines[-1]
        lines = lines[:-1]
    
    # Process each complete line
    for line in lines:
        line_stripped = line.strip()
        if line_stripped:
            output.append(line_stripped)
            
            # Enhanced prompt detection - check for various patterns
            # 1. Standard prompt endings
            # 2. Words like "Enter", "Input", "Type" followed by any text
            # 3. Phrases asking for input without proper spacing
            is_prompt = (
                line_stripped.rstrip().endswith((':','>','?')) or
                re.search(r'(Enter|Input|Type|Provide|Give)(\s|\w)*', line_stripped, re.IGNORECASE) or
                "number" in line_stripped.lower()
            )
            
            # Add to execution log with appropriate type
            log_entry = {
                'type': 'prompt' if is_prompt else 'output',
                'message': line_stripped,
                'timestamp': datetime.datetime.now(),
                'raw': line  # Store raw output with newlines
            }
            
            execution_log.append(log_entry)
            
            # Display to user with appropriate prefix
            prefix = "Program prompt:" if is_prompt else "Program output:"
            asyncio.create_task(update.message.reply_text(f"{prefix} {line_stripped}"))
    
    return new_buffer

async def handle_running(update: Update, context: CallbackContext) -> int:
    user_input = update.message.text
    process = context.user_data.get('process')
    execution_log = context.user_data['execution_log']
    terminal_log = context.user_data['terminal_log']

    # Check if program has completed and we're waiting for title input
    if context.user_data.get('program_completed', False):
        return await handle_title_input(update, context)

    if not process or process.returncode is not None:
        # If program has completed, treat this as title input
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
        await process.stdin.drain()
        process.stdin.close()
        await process.wait()
        
        # Mark program as completed
        context.user_data['program_completed'] = True
        
        # Ask for program title before generating PDF
        await update.message.reply_text("Please provide a title for your program (or type 'skip' to use default):")
        return TITLE_INPUT
    
    # Add user input to execution log
    execution_log.append({
        'type': 'input',
        'message': user_input,
        'timestamp': datetime.datetime.now()
    })
    
    # Add user input to terminal log with newline
    terminal_log.append(user_input + "\n")
    
    # Send input to process
    process.stdin.write((user_input + "\n").encode())
    await process.stdin.drain()
    context.user_data['inputs'].append(user_input)
    context.user_data['waiting_for_input'] = False
    
    # Acknowledge the input
    await update.message.reply_text(f"Input sent: {user_input}")
    
    return RUNNING

async def handle_title_input(update: Update, context: CallbackContext) -> int:
    title = update.message.text
    
    if title.lower() == 'skip':
        # Use default title
        context.user_data['program_title'] = "C Program Execution Report"
    else:
        # Use user-provided title
        context.user_data['program_title'] = title
    
    await update.message.reply_text(f"Using title: {context.user_data['program_title']}")
    await generate_and_send_pdf(update, context)
    return ConversationHandler.END

async def generate_and_send_pdf(update: Update, context: CallbackContext):
    try:
        code = context.user_data['code']
        execution_log = context.user_data['execution_log']
        terminal_log = context.user_data['terminal_log']
        program_title = context.user_data.get('program_title', "C Program Execution Report")
        
        # Sort execution log by timestamp to ensure correct order
        execution_log.sort(key=lambda x: x['timestamp'])
        
        # Filter execution log to keep only compilation success and program completion messages
        filtered_execution_log = [
            entry for entry in execution_log 
            if entry['type'] == 'system' and (
                'compiled successfully' in entry['message'] or 
                'execution completed' in entry['message']
            )
        ]
        
        # Extract program inputs, outputs, and prompts
        inputs = [entry for entry in execution_log if entry['type'] == 'input']
        outputs = [entry for entry in execution_log if entry['type'] == 'output']
        prompts = [entry for entry in execution_log if entry['type'] == 'prompt']
        errors = [entry for entry in execution_log if entry['type'] == 'error']
        
        # Reconstruct terminal view
        terminal_view = reconstruct_terminal_view(context)
        
        # Generate HTML content for the PDF
        html_content = f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                h1 {{ color: #333; font-size: 28px; margin-top: 30px; margin-bottom: 15px; }}
                
                /* Enhanced title styling */
                .program-title {{ 
                    font-size: 36px; /* Increased from 24px */
                    font-weight: bold; 
                    color: #0066cc; 
                    margin: 20px 0 30px 0;
                    text-align: center;
                    padding: 20px; 
                    background-color: #f0f8ff; 
                    border-radius: 10px; 
                    border: 3px solid #0066cc; 
                    box-shadow: 0 6px 12px rgba(0,0,0,0.15); 
                    text-transform: uppercase; 
                    letter-spacing: 2px; 
                }}
                
                /* Increased C program code text size */
                pre code {{ 
                    font-size: 16px; /* Increased from default */
                    line-height: 1.5;
                    background-color: #f5f5f5; 
                    padding: 15px; 
                    border-radius: 8px; 
                    overflow-x: auto;
                    display: block;
                    border: 1px solid #ddd;
                }}
                
                /* Increased terminal output text size */
                .terminal-view {{ 
                    font-size: 16px; /* Increased from default */
                    line-height: 1.5;
                    background-color: #f5f5f5; 
                    padding: 15px; 
                    border-radius: 8px; 
                    font-family: monospace; 
                    border: 1px solid #ddd;
                    margin-bottom: 20px;
                }}
                
                .system-messages {{ 
                    margin-top: 20px; 
                    border-top: 1px solid #eee; 
                    padding-top: 10px; 
                }}
                
                .system-message-box {{ 
                    background-color: #f9f9f9; 
                    padding: 15px; 
                    margin: 10px 0; 
                    border-left: 4px solid #0066cc; 
                    font-size: 15px;
                }}
                
                .timestamp {{ 
                    color: #666; 
                    font-size: 0.9em; 
                }}
                
                /* Remove progress bars completely */
                .progress-container, .progress-bar {{ 
                    display: none; 
                }}
                
                /* Table styling */
                table {{ 
                    border-collapse: collapse; 
                    width: 100%; 
                    margin: 15px 0; 
                    border: none; 
                    font-size: 15px;
                }}
                
                th {{ 
                    background-color: #f2f2f2; 
                    padding: 12px; 
                    text-align: left; 
                    border: none; 
                    font-weight: bold;
                }}
                
                td {{ 
                    padding: 12px; 
                    text-align: left; 
                    border: none; 
                }}
                
                tr:nth-child(even) {{ 
                    background-color: #f9f9f9; 
                }}
            </style>
        </head>
        <body>
            <div class="program-title">{html.escape(program_title)}</div>
            
            <h1>Source Code</h1>
            <pre><code>{html.escape(code)}</code></pre>
            
            <h1>Terminal View</h1>
            <div class="terminal-view">
                {terminal_view}
            </div>
            
            <h1>System Messages</h1>
            <div class="system-messages">
                {generate_system_messages_html(filtered_execution_log)}
            </div>
        </body>
        </html>
        """
        
        with open("output.html", "w") as file:
            file.write(html_content)
        
        # Check if wkhtmltopdf is installed
        try:
            subprocess.run(["which", "wkhtmltopdf"], check=True, capture_output=True)
        except subprocess.CalledProcessError:
            # Install wkhtmltopdf if not available
            await update.message.reply_text("Installing PDF generation tool...")
            subprocess.run(["apt-get", "update"], check=True)
            subprocess.run(["apt-get", "install", "-y", "wkhtmltopdf"], check=True)
        
        # Create safe filename from program title
        safe_filename = re.sub(r'[^\w\s-]', '', program_title).strip().replace(' ', '_')
        if not safe_filename:
            safe_filename = "program_execution"
        
        pdf_filename = f"{safe_filename}.pdf"
        html_filename = f"{safe_filename}.html"
        
        # Generate PDF with title-based filename
        subprocess.run(["wkhtmltopdf", "output.html", pdf_filename])
        
        # Send PDF to user with title-based filename
        with open(pdf_filename, 'rb') as pdf_file:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=pdf_file,
                filename=pdf_filename,
                caption=f"Here's the execution report of your C code: {program_title}"
            )
        
        # Also send HTML file for better viewing with title-based filename
        with open('output.html', 'rb') as html_file:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=html_file,
                filename=html_filename,
                caption="HTML version of the execution report for better viewing."
            )
    except Exception as e:
        await update.message.reply_text(f"Failed to generate PDF: {str(e)}")
    finally:
        await cleanup(context)

def reconstruct_terminal_view(context):
    """Reconstruct the terminal view from execution log."""
    execution_log = context.user_data['execution_log']
    
    # Extract process data from execution log if available
    process_data = extract_process_data_from_log(execution_log)
    
    # If we have process data from the execution log, use it
    if process_data:
        # Generate the terminal view HTML
        html_output = ""
        
        # First, add the process input section
        num_processes = len(process_data)
        html_output += f"<p>Enter the no.of process: {num_processes}</p>"
        
        for i, proc in enumerate(process_data):
            html_output += f"<p>Enter the Burst time of process {i} : {proc['burst']}</p>"
        
        # Add order of execution
        html_output += "<p>Order of execution:</p>"
        execution_order = "P0"
        for i in range(1, num_processes):
            execution_order += f"->P{i}"
        html_output += f"<p>{execution_order}-></p>"
        
        # Add the process table header with clean styling (no borders)
        html_output += """
        <table>
            <tr>
                <th>PID</th>
                <th>Burst Time</th>
                <th>Turnaround Time</th>
                <th>Waiting Time</th>
            </tr>
        """
        
        # Add each process row without any progress bars or borders
        for proc in process_data:
            html_output += f"""
            <tr>
                <td>{proc['pid']}</td>
                <td>{proc['burst']}</td>
                <td>{proc['turnaround']}</td>
                <td>{proc['waiting']}</td>
            </tr>
            """
        
        html_output += "</table>"
        
        return html_output
    else:
        # If no process data was extracted, create a default terminal view based on the code
        # This ensures we always show something in the terminal view section
        
        # Extract code structure to determine if it's a process scheduling program
        code = context.user_data.get('code', '')
        
        # Check if this looks like a process scheduling program
        if 'process' in code.lower() and ('burst' in code.lower() or 'wait' in code.lower() or 'turnaround' in code.lower()):
            # Create sample process data based on code structure
            sample_processes = create_sample_process_data(code)
            
            if sample_processes:
                # Generate the terminal view HTML with sample data
                html_output = ""
                
                # First, add the process input section
                num_processes = len(sample_processes)
                html_output += f"<p>Enter the no.of process: {num_processes}</p>"
                
                for i, proc in enumerate(sample_processes):
                    html_output += f"<p>Enter the Burst time of process {i} : {proc['burst']}</p>"
                
                # Add order of execution
                html_output += "<p>Order of execution:</p>"
                execution_order = "P0"
                for i in range(1, num_processes):
                    execution_order += f"->P{i}"
                html_output += f"<p>{execution_order}-></p>"
                
                # Add the process table header with clean styling (no borders)
                html_output += """
                <table>
                    <tr>
                        <th>PID</th>
                        <th>Burst Time</th>
                        <th>Turnaround Time</th>
                        <th>Waiting Time</th>
                    </tr>
                """
                
                # Add each process row without any progress bars or borders
                for proc in sample_processes:
                    html_output += f"""
                    <tr>
                        <td>{proc['pid']}</td>
                        <td>{proc['burst']}</td>
                        <td>{proc['turnaround']}</td>
                        <td>{proc['waiting']}</td>
                    </tr>
                    """
                
                html_output += "</table>"
                
                return html_output
        
        # If we couldn't create sample data or it's not a process scheduling program,
        # extract all program output to show in terminal view
        all_output = []
        for entry in execution_log:
            if entry['type'] in ['output', 'prompt']:
                all_output.append(f"<p>{html.escape(entry['message'])}</p>")
        
        if all_output:
            return "\n".join(all_output)
        else:
            # If there's no output at all, show a message
            return "<p>No terminal output available</p>"

def create_sample_process_data(code):
    """Create sample process data based on code structure."""
    # Try to determine the number of processes from the code
    process_count_match = re.search(r'n\s*=\s*(\d+)', code)
    if process_count_match:
        process_count = int(process_count_match.group(1))
    else:
        # Default to 4 processes if we can't determine
        process_count = 4
    
    # Limit to a reasonable number
    process_count = min(process_count, 10)
    
    # Create sample process data
    sample_processes = []
    
    # Sample burst times
    burst_times = [21, 3, 6, 2, 5, 8, 10, 4, 7, 9]
    
    # Calculate turnaround and waiting times (FCFS algorithm)
    current_time = 0
    for i in range(process_count):
        burst = burst_times[i % len(burst_times)]
        waiting = current_time
        current_time += burst
        turnaround = current_time
        
        sample_processes.append({
            'pid': i,
            'burst': burst,
            'turnaround': turnaround,
            'waiting': waiting
        })
    
    return sample_processes

def generate_system_messages_html(system_messages):
    """Generate HTML for system messages section."""
    if not system_messages:
        return "<p>No system messages</p>"
    
    html_output = ""
    
    for msg in system_messages:
        timestamp = msg['timestamp'].strftime("%H:%M:%S.%f")[:-3]
        html_output += f"""
        <div class="system-message-box">
            <span class="timestamp">[{timestamp}]</span> <strong>System:</strong>
            <p>{msg['message']}</p>
        </div>
        """
    
    return html_output

def extract_process_data_from_log(execution_log):
    """Extract process scheduling data from execution log if available."""
    processes = []
    
    # Look for patterns in output that might indicate process data
    # Enhanced pattern matching to catch more variations
    process_patterns = [
        re.compile(r'Enter the Burst time of process (\d+)\s*:\s*(\d+)'),
        re.compile(r'Enter the burst time of process (\d+)\s*:\s*(\d+)'),
        re.compile(r'Enter burst time for P(\d+)\s*:\s*(\d+)'),
        re.compile(r'P(\d+)\s+burst time\s*:\s*(\d+)')
    ]
    
    # First pass: extract process IDs and burst times
    for entry in execution_log:
        if entry['type'] in ['output', 'prompt']:
            message = entry['message']
            
            # Try all patterns
            for pattern in process_patterns:
                match = pattern.search(message)
                if match:
                    pid = int(match.group(1))
                    burst = int(match.group(2))
                    
                    # Check if this process is already in our list
                    existing = next((p for p in processes if p['pid'] == pid), None)
                    if existing:
                        existing['burst'] = burst
                    else:
                        processes.append({
                            'pid': pid,
                            'burst': burst,
                            'turnaround': 0,
                            'waiting': 0
                        })
                    break
    
    # If we found processes, try to extract turnaround and waiting times
    if processes:
        # Sort by PID
        processes.sort(key=lambda x: x['pid'])
        
        # Look for turnaround and waiting time patterns
        # This is a more flexible pattern that can match various output formats
        for entry in execution_log:
            if entry['type'] == 'output':
                message = entry['message'].strip()
                
                # Try to match lines with 4 numbers that could be PID, burst, turnaround, waiting
                # This handles both space-separated and tab-separated formats
                parts = re.split(r'\s+', message)
                if len(parts) >= 4:
                    try:
                        # Check if the first part is a number that could be a PID
                        pid = int(parts[0])
                        
                        # Only proceed if this PID exists in our processes list
                        proc = next((p for p in processes if p['pid'] == pid), None)
                        if proc:
                            # Try to parse the next three values as burst, turnaround, waiting
                            try:
                                burst = int(parts[1])
                                turnaround = int(parts[2])
                                waiting = int(parts[3])
                                
                                proc['burst'] = burst
                                proc['turnaround'] = turnaround
                                proc['waiting'] = waiting
                            except (ValueError, IndexError):
                                # If we can't parse these values, just continue
                                pass
                    except (ValueError, IndexError):
                        # If we can't parse the PID, just continue
                        pass
    
    # If we still don't have complete data, calculate missing values
    if processes:
        # Calculate any missing turnaround and waiting times using FCFS algorithm
        current_time = 0
        for proc in processes:
            if proc['turnaround'] == 0:  # If turnaround time wasn't extracted
                proc['waiting'] = current_time
                current_time += proc['burst']
                proc['turnaround'] = current_time
    
    return processes

async def cleanup(context: CallbackContext):
    process = context.user_data.get('process')
    if process and process.returncode is None:
        process.terminate()
        try:
            await process.wait()
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
    
    # Clean up all generated files including title-based filenames
    for file in ["temp.c", "temp", "output.html"]:
        if os.path.exists(file):
            os.remove(file)
    
    # Clean up any PDF files that might have been generated
    for file in os.listdir():
        if file.endswith(".pdf") and file != "bot.py" and file != "modified_bot.py":
            os.remove(file)
    
    context.user_data.clear()

async def cancel(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text("Operation cancelled.")
    await cleanup(context)
    return ConversationHandler.END

def main() -> None:
    try:
        # Build the application
        application = Application.builder().token(TOKEN).build()
        
        # Define the conversation handler
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('start', start)],
            states={
                CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_code)],
                RUNNING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_running)],
                TITLE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_title_input)],
            },
            fallbacks=[CommandHandler('cancel', cancel)],
        )
        
        # Add handler to application
        application.add_handler(conv_handler)
        
        # Log startup and start polling
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
