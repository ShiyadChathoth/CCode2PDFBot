
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
CODE, RUNNING = range(2)

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
                    
                    await update.message.reply_text("Program execution completed.")
                    await generate_and_send_pdf(update, context)
                    break
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
                execution_log.append({
                    'type': 'system',
                    'message': 'Program appears to be waiting for input. Please provide input (or type "done" to finish):',
                    'timestamp': datetime.datetime.now()
                })
                await update.message.reply_text("Program appears to be waiting for input. Please provide input (or type 'done' to finish):")
            
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
            
            await update.message.reply_text("Program execution completed.")
            await generate_and_send_pdf(update, context)
            break

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

    if not process or process.returncode is not None:
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
        await generate_and_send_pdf(update, context)
        return ConversationHandler.END
    
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

async def generate_and_send_pdf(update: Update, context: CallbackContext):
    try:
        code = context.user_data['code']
        execution_log = context.user_data['execution_log']
        terminal_log = context.user_data['terminal_log']
        
        # Sort execution log by timestamp to ensure correct order
        execution_log.sort(key=lambda x: x['timestamp'])
        
        # Filter execution log to keep only compilation success and program completion messages
        filtered_execution_log = [
            entry for entry in execution_log 
            if entry['type'] == 'system' and (
                entry['message'] == 'Code compiled successfully!' or 
                entry['message'] == 'Code compiled successfully after aggressive whitespace cleaning!' or
                entry['message'] == 'Program execution completed.'
            )
        ]
        
        # Create a more detailed HTML with syntax highlighting and better formatting
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>C Program Execution Report</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                h1 {{ color: #2c3e50; border-bottom: 1px solid #eee; padding-bottom: 10px; }}
                h2 {{ color: #3498db; margin-top: 20px; }}
                pre {{ background-color: #f8f9fa; padding: 15px; border-radius: 5px; overflow-x: auto; }}
                code {{ 
                    font-family: Consolas, Monaco, 'Andale Mono', monospace; 
                    tab-size: 4;
                    -moz-tab-size: 4;
                    -o-tab-size: 4;
                }}
                .source-code {{ 
                    page-break-inside: avoid; /* Try to keep source code on one page */
                    max-height: 800px; /* Limit height to fit on one page */
                    overflow-y: auto; /* Add scrollbar if needed */
                    background-color: #f8f9fa; 
                    padding: 15px; 
                    border-radius: 5px; 
                    font-size: 0.9em; /* Slightly smaller font to fit more code */
                    line-height: 1.4;
                    white-space: pre;
                }}
                .terminal {{ 
                    font-family: 'Courier New', monospace; 
                    white-space: pre; 
                    line-height: 1.5;
                    margin: 0;
                    tab-size: 8;
                    -moz-tab-size: 8;
                    -o-tab-size: 8;
                    padding: 15px;
                    background-color: #f8f9fa;
                    border-radius: 5px;
                    letter-spacing: 0;
                    word-spacing: 0;
                    font-size: 14px;
                    font-variant-ligatures: none;
                    font-feature-settings: "tnum";
                    font-variant-numeric: tabular-nums;
                    -webkit-font-feature-settings: "tnum";
                    -moz-font-feature-settings: "tnum";
                }}
                
                /* Special style for table content */
                .terminal-table {{ 
                    font-family: 'Courier New', monospace;
                    border-collapse: collapse;
                    width: 100%;
                    margin: 0;
                    table-layout: fixed;
                    background-color: transparent !important;
                }}
                /* Fixed column widths for FCFS table */
                .terminal-table col.col-pid {{ width: 50px; }}
                .terminal-table col.col-burst {{ width: 100px; }}
                .terminal-table col.col-turnaround {{ width: 150px; }}
                .terminal-table col.col-waiting {{ width: 120px; }}
                .terminal-table th, .terminal-table td {{ 
                    text-align: left;
                    padding: 0 8px;
                    font-size: 14px;
                    font-feature-settings: "tnum";
                    font-variant-numeric: tabular-nums;
                    white-space: nowrap;
                    overflow: hidden;
                    background-color: transparent !important;
                    border: none;
                }}
                .terminal-table tr {{ 
                    background-color: transparent !important;
                    background: none !important;
                }}
                .terminal-table tr:nth-child(odd) {{ 
                    background-color: transparent !important;
                    background: none !important;
                }}
                .terminal-table tr:nth-child(even) {{ 
                    background-color: transparent !important;
                    background: none !important;
                }}
                .terminal-table tbody {{ 
                    background-color: transparent !important;
                    background: none !important;
                }}
                .terminal-table th {{ 
                    font-weight: normal;
                    border-bottom: none;
                    background-color: transparent !important;
                    background: none !important;
                }}
                .terminal-table td {{ 
                    border-bottom: none;
                    background-color: transparent !important;
                    background: none !important;
                }}
                .system {{ background-color: #f5f5f5; padding: 10px; border-left: 4px solid #7f8c8d; margin: 10px 0; white-space: pre-wrap; }}
                .timestamp {{ color: #7f8c8d; font-size: 0.8em; }}
                
                @media print {{
                    .source-code {{ 
                        page-break-inside: avoid;
                        max-height: none; /* Remove height limit for printing */
                    }}
                    .terminal {{ page-break-before: always; }}
                    h2 {{ page-break-before: always; }}
                }}
            </style>
        </head>
        <body>
            <h1>C Program Execution Report</h1>
            
            <h2>Source Code</h2>
            <pre class="source-code"><code>{html.escape(code)}</code></pre>
            
            <h2>Terminal View</h2>
            <pre class="terminal">"""
        
        # Combine all terminal log entries to create exact terminal output
        terminal_content = ""
        for entry in terminal_log:
            terminal_content += entry
        
        # Process terminal content to detect and convert table-like structures to HTML tables
        processed_content = ""
        lines = terminal_content.split('\n')
        in_table = False
        table_buffer = []
        table_header = []
        
        for line in lines:
            # Detect table headers or rows by checking for multiple spaces or tabs between words
            # Common patterns in FCFS tables like "PID Burst Time Turnaround Time waiting Time"
            is_table_header = bool('PID' in line and ('Burst Time' in line or 'Turnaround Time' in line or 'waiting Time' in line))
            is_table_row = bool(re.match(r'^\s*\d+\s+\d+\s+\d+\s+\d+\s*$', line) or 
                              (re.search(r'\S+\s{2,}\S+', line) and not is_table_header))
            
            if is_table_header or is_table_row:
                if not in_table:
                    in_table = True
                    # If starting a new table, add any previous content
                    if processed_content:
                        html_content += html.escape(processed_content)
                        processed_content = ""
                    
                # Start a new HTML table with colgroup for fixed column widths
                    html_content += '</pre><table class="terminal-table" style="border-collapse: collapse; width: 100%; background-color: transparent !important; background: none !important; border-spacing: 0;">'
                    html_content += '<colgroup>'
                    html_content += '<col class="col-pid">'
                    html_content += '<col class="col-burst">'
                    html_content += '<col class="col-turnaround">'
                    html_content += '<col class="col-waiting">'
                    html_content += '</colgroup>'
                    
                    # If this is a header row, process it specially
                    if is_table_header:
                        # For FCFS tables, use fixed column headers to ensure alignment
                        html_content += '<thead><tr style="background-color: transparent !important; background: none !important;">'
                        html_content += '<th style="text-align: left; padding: 0 8px; font-weight: normal; background-color: transparent !important; background: none !important;">PID</th>'
                        html_content += '<th style="text-align: left; padding: 0 8px; font-weight: normal; background-color: transparent !important; background: none !important;">Burst Time</th>'
                        html_content += '<th style="text-align: left; padding: 0 8px; font-weight: normal; background-color: transparent !important; background: none !important;">Turnaround Time</th>'
                        html_content += '<th style="text-align: left; padding: 0 8px; font-weight: normal; background-color: transparent !important; background: none !important;">waiting Time</th>'
                        html_content += '</tr></thead><tbody>'
                        table_header = ['PID', 'Burst Time', 'Turnaround Time', 'waiting Time']
                        continue  # Skip adding this line to the table buffer
                
                # For data rows, add to table buffer
                if is_table_row:
                    table_buffer.append(line.strip())
            else:
                if in_table:
                    in_table = False
                    # Process and add the table content as HTML table rows
                    if table_buffer:
                        for row in table_buffer:
                            # For FCFS tables, ensure consistent column count
                            # Split the row by any whitespace
                            cells = re.split(r'\s+', row.strip())
                            
                            # Ensure we have exactly 4 cells
                            while len(cells) < 4:
                                cells.append("")
                            
                            # Only use the first 4 cells
                            if len(cells) > 4:
                                cells = cells[:4]
                                
                            html_content += '<tr style="background-color: transparent !important; background: none !important;">'
                            for cell in cells:
                                html_content += f'<td style="text-align: left; padding: 0 8px; font-weight: normal; background-color: transparent !important; background: none !important;">{html.escape(cell.strip())}</td>'
                            html_content += '</tr>'
                        table_buffer = []
                    
                    # Close table and start normal terminal section
                    html_content += '</tbody></table><pre class="terminal">'
                
                # Add non-table line to regular content buffer
                processed_content += line + '\n'
        
        # Handle any remaining content
        if in_table and table_buffer:
            # Process any remaining table rows
            for row in table_buffer:
                # Split the row by any whitespace
                cells = re.split(r'\s+', row.strip())
                
                # Ensure we have exactly 4 cells
                while len(cells) < 4:
                    cells.append("")
                
                # Only use the first 4 cells
                if len(cells) > 4:
                    cells = cells[:4]
                    
                html_content += '<tr style="background-color: transparent !important; background: none !important;">'
                for cell in cells:
                    html_content += f'<td style="text-align: left; padding: 0 8px; font-weight: normal; background-color: transparent !important; background: none !important;">{html.escape(cell.strip())}</td>'
                html_content += '</tr>'
            html_content += '</tbody></table>'
        elif processed_content:
            html_content += html.escape(processed_content)
        
        html_content += """</pre>
        """
        
        # Add only the system messages for compilation success and program completion
        if filtered_execution_log:
            html_content += """
            <h2>System Messages</h2>
            <div class="execution-flow">
            """
            
            for entry in filtered_execution_log:
                timestamp = entry['timestamp'].strftime('%H:%M:%S.%f')[:-3]  # Include milliseconds
                html_content += f'<div class="system"><span class="timestamp">[{timestamp}]</span> <strong>System:</strong> <pre>{html.escape(entry["message"])}</pre></div>\n'
            
            html_content += """
            </div>
            """
        
        html_content += """
        </body>
        </html>
        """
        
        with open("output.html", "w") as file:
            file.write(html_content)
        
        # Generate PDF with wkhtmltopdf with specific options for better layout
        pdf_process = subprocess.run(
            [
                "wkhtmltopdf",
                "--enable-local-file-access",
                "--page-size", "A4",
                "--margin-top", "10mm",
                "--margin-bottom", "10mm",
                "--margin-left", "10mm",
                "--margin-right", "10mm",
                "--disable-smart-shrinking",  # Prevents unexpected scaling
                "output.html",
                "output.pdf"
            ],
            capture_output=True,
            text=True
        )
        
        if pdf_process.returncode != 0:
            logger.error(f"PDF generation failed: {pdf_process.stderr}")
            await update.message.reply_text("Failed to generate PDF report. Sending HTML instead.")
            with open('output.html', 'rb') as html_file:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id, 
                    document=html_file,
                    filename="program_execution.html"
                )
        else:
            # Send both PDF and HTML for maximum compatibility
            await update.message.reply_text("Generating execution report...")
            with open('output.pdf', 'rb') as pdf_file:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id, 
                    document=pdf_file,
                    filename="program_execution.pdf"
                )
            
            with open('output.html', 'rb') as html_file:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id, 
                    document=html_file,
                    filename="program_execution.html"
                )
        
    except Exception as e:
        logger.error(f"Error in PDF generation: {str(e)}")
        await update.message.reply_text(f"Failed to generate report: {str(e)}")
    finally:
        await cleanup(context)

async def cleanup(context: CallbackContext):
    process = context.user_data.get('process')
    if process and process.returncode is None:
        try:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        except Exception as e:
            logger.error(f"Error during process cleanup: {str(e)}")
    
    # Clean up temporary files
    for file in ["temp.c", "temp", "output.pdf", "output.html"]:
        if os.path.exists(file):
            try:
                os.remove(file)
            except Exception as e:
                logger.error(f"Error removing file {file}: {str(e)}")
    
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

