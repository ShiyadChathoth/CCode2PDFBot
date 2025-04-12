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
CODE, RUNNING, TITLE_INPUT = range(3)

async def start(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text(
        'Hi! Send me your C code, and I will compile and execute it step-by-step.\n\n'
        'You can provide multiple inputs at once by separating them with spaces.\n'
        'Example: "5 10 15" to send three separate inputs.'
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
            "⚠️ I detected and fixed non-standard whitespace characters in your code that would cause compilation errors."
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
    context.user_data['message_queue'] = []  # Queue for ordered message processing
    context.user_data['processing_message'] = False  # Flag to indicate we're processing messages
    
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
            
            # Start message processor
            asyncio.create_task(process_message_queue(update, context))
            
            # Start output reader
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
                    
                    # Start message processor
                    asyncio.create_task(process_message_queue(update, context))
                    
                    # Start output reader
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

def extract_printf_statements(code):
    """Extract potential printf prompt patterns from the code."""
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

async def process_message_queue(update, context):
    """Process messages in the queue to ensure proper ordering."""
    while True:
        if context.user_data.get('program_completed', False) and not context.user_data['message_queue']:
            # Program is done and queue is empty, we can exit
            return
            
        if context.user_data['message_queue']:
            # Process one message at a time
            context.user_data['processing_message'] = True
            message_data = context.user_data['message_queue'].pop(0)
            
            try:
                await update.message.reply_text(message_data['text'])
            except Exception as e:
                logger.error(f"Failed to send message: {e}")
                
            context.user_data['processing_message'] = False
            
            # Short delay between messages for better readability
            await asyncio.sleep(0.1)
        else:
            # No messages to process, sleep briefly
            await asyncio.sleep(0.05)

def add_to_message_queue(context, text):
    """Add a message to the ordered processing queue."""
    context.user_data['message_queue'].append({
        'text': text,
        'timestamp': datetime.datetime.now()
    })

async def read_process_output(update: Update, context: CallbackContext):
    process = context.user_data['process']
    output = context.user_data['output']
    errors = context.user_data['errors']
    execution_log = context.user_data['execution_log']
    output_buffer = context.user_data['output_buffer']
    terminal_log = context.user_data['terminal_log']
    
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
                    
                    # Send the completion message
                    add_to_message_queue(context, "Program execution completed.")
                    
                    # Wait a bit more before asking for title
                    await asyncio.sleep(0.8)
                    
                    add_to_message_queue(context, "Please provide a title for your program (or type 'skip' to use default):")
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
                    
                    input_message = f"Program is waiting for input: \"{last_prompt}\"\nPlease provide input (multiple inputs can be separated by spaces, or type 'done' to finish):"
                    
                    execution_log.append({
                        'type': 'system',
                        'message': input_message,
                        'timestamp': datetime.datetime.now()
                    })
                    add_to_message_queue(context, input_message)
            
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
                    
                    add_to_message_queue(context, f"Error: {line.strip()}")

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
            
            # Send completion message
            add_to_message_queue(context, "Program execution completed.")
            
            # Wait a bit more before asking for title
            await asyncio.sleep(0.8)
            
            add_to_message_queue(context, "Please provide a title for your program (or type 'skip' to use default):")
            return TITLE_INPUT

def process_output_chunk(context, buffer, update):
    """Process the output buffer, preserving tabs and whitespace with improved printf prompt detection."""
    execution_log = context.user_data['execution_log']
    output = context.user_data['output']
    printf_patterns = context.user_data.get('printf_patterns', [])
    
    # First check if the entire buffer might be a prompt without a newline
    if buffer and not buffer.endswith('\n'):
        is_prompt, prompt_text = detect_prompt(buffer, printf_patterns)
        if is_prompt:
            context.user_data['last_prompt'] = prompt_text
            
            log_entry = {
                'type': 'prompt',
                'message': buffer,
                'timestamp': datetime.datetime.now(),
                'raw': buffer
            }
            
            execution_log.append(log_entry)
            add_to_message_queue(context, f"Program prompt: {buffer}")
            
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
            is_prompt, prompt_text = detect_prompt(line_stripped, printf_patterns)
            
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
            add_to_message_queue(context, f"{prefix} {line_stripped}")
    
    return new_buffer

def detect_prompt(line, printf_patterns):
    """Enhanced detection for printf prompts. Returns (is_prompt, prompt_text)"""
    line_text = line.strip()
    
    # First check if the line matches or closely matches any extracted printf patterns
    for pattern in printf_patterns:
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

    # If program is already completed, treat this as title input
    if context.user_data.get('program_completed', False):
        return await handle_title_input(update, context)

    if not process or process.returncode is not None:
        if context.user_data.get('program_completed', False):
            return await handle_title_input(update, context)
        else:
            add_to_message_queue(context, "Program is not running anymore.")
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
        
        # Add delay to ensure all output is processed
        await asyncio.sleep(1)
        
        context.user_data['program_completed'] = True
        
        add_to_message_queue(context, "Please provide a title for your program (or type 'skip' to use default):")
        return TITLE_INPUT
    
    # Handle multiple inputs (split by spaces)
    inputs = user_input.split()
    
    # Set the flag that we're sending input to prevent output processing during this time
    context.user_data['is_sending_input'] = True
    
    if len(inputs) > 1:
        add_to_message_queue(context, f"Processing multiple inputs: {', '.join(inputs)}")
    
    for idx, single_input in enumerate(inputs):
        execution_log.append({
            'type': 'input',
            'message': single_input,
            'timestamp': datetime.datetime.now()
        })
        
        terminal_log.append(single_input + "\n")
        
        # Add message about which input we're sending (only for multiple inputs)
        if len(inputs) > 1:
            add_to_message_queue(context, f"Input {idx+1}/{len(inputs)}: {single_input}")
        else:
            add_to_message_queue(context, f"Input sent: {single_input}")
        
        # Send the input to the process
        process.stdin.write((single_input + "\n").encode())
        await process.stdin.drain()
        context.user_data['inputs'].append(single_input)
        
        # For multiple inputs, wait a bit between inputs
        if idx < len(inputs) - 1:
            await asyncio.sleep(0.3)
    
    # Reset flags
    context.user_data['waiting_for_input'] = False
    
    # Add a small delay to ensure message ordering
    await asyncio.sleep(0.2)
    
    # Reset the input sending flag
    context.user_data['is_sending_input'] = False
    
    return RUNNING

async def handle_title_input(update: Update, context: CallbackContext) -> int:
    title = update.message.text
    
    if title.lower() == 'skip':
        context.user_data['program_title'] = "C Program Execution Report"
    else:
        context.user_data['program_title'] = title
    
    add_to_message_queue(context, f"Using title: {context.user_data['program_title']}")
    
    # Wait for message queue to process this message
    while context.user_data['message_queue']:
        await asyncio.sleep(0.1)
    
    await generate_and_send_pdf(update, context)
    return ConversationHandler.END

async def check_system_capabilities(update):
    """Check and report if required tools are available"""
    try:
        # Check for wkhtmltopdf
        result = subprocess.run(["which", "wkhtmltopdf"], capture_output=True, text=True)
        wkhtmltopdf_path = result.stdout.strip()
        
        if not wkhtmltopdf_path:
            await update.message.reply_text("Error: wkhtmltopdf is not installed on this system.")
            return False
            
        # Check if we can write to current directory
        test_file = "write_test.txt"
        try:
            with open(test_file, "w") as f:
                f.write("test")
            os.remove(test_file)
        except Exception as e:
            await update.message.reply_text(f"Error: Cannot write to current directory: {str(e)}")
            return False
            
        # Log system information
        logger.info(f"wkhtmltopdf found at: {wkhtmltopdf_path}")
        logger.info(f"Current working directory: {os.getcwd()}")
        logger.info(f"Directory is writable: Yes")
        
        return True
        
    except Exception as e:
        await update.message.reply_text(f"Error checking system capabilities: {str(e)}")
        return False

async def generate_and_send_pdf(update: Update, context: CallbackContext):
    # First check system capabilities
    if not await check_system_capabilities(update):
        return
        
    try:
        code = context.user_data['code']
        execution_log = context.user_data['execution_log']
        terminal_log = context.user_data['terminal_log']
        program_title = context.user_data.get('program_title', "C Program Execution Report")

        # Generate sanitized filename from title
        # Replace invalid filename characters with underscores and ensure it ends with .pdf
        sanitized_title = re.sub(r'[\\/*?:"<>|]', "_", program_title)
        sanitized_title = re.sub(r'\s+', "_", sanitized_title)  # Replace spaces with underscores
        pdf_filename = os.path.abspath(f"{sanitized_title}.pdf")
        
        logger.info(f"Generating PDF with filename: {pdf_filename}")

        # Generate HTML with proper tab alignment styling
        html_content = f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                .program-title {{
                    font-size: 30px;
                    font-weight: bold;
                    text-align: center;
                    margin-bottom: 20px;
                    text-decoration: underline;
                    text-decoration-thickness: 5px;
                    border-bottom: 3px
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
                }}
                .terminal-view {{
                    margin: 10px 0;
                }}
            </style>
        </head>
        <body>
            <div class="program-title">{html.escape(program_title)}</div>
            <pre><code>{html.escape(code)}</code></pre>
            <div class="terminal-view">
                {reconstruct_terminal_view(context)}
            </div>
        </body>
        </html>
        """

        with open("output.html", "w") as file:
            file.write(html_content)

        # Generate PDF with better error handling
        try:
            result = subprocess.run(
                ["wkhtmltopdf", "output.html", pdf_filename],
                capture_output=True,
                text=True,
                timeout=30  # 30-second timeout
            )
            
            if result.returncode != 0:
                logger.error(f"wkhtmltopdf error: {result.stderr}")
                await update.message.reply_text(f"PDF generation failed: {result.stderr}")
                
                # Try alternative PDF generation
                await generate_and_send_pdf_alternative(update, context)
                return
        except subprocess.TimeoutExpired:
            logger.error("wkhtmltopdf process timed out")
            await update.message.reply_text("PDF generation timed out. Trying alternative method...")
            
            # Try alternative PDF generation
            await generate_and_send_pdf_alternative(update, context)
            return

        # Check if file exists before sending
        if not os.path.exists(pdf_filename):
            logger.error(f"PDF file not found at: {pdf_filename}")
            await update.message.reply_text("PDF file was not created. Trying alternative method...")
            
            # Try alternative PDF generation
            await generate_and_send_pdf_alternative(update, context)
            return
            
        logger.info(f"PDF file created successfully at: {pdf_filename}, size: {os.path.getsize(pdf_filename)} bytes")

        # Send PDF to user
        with open(pdf_filename, 'rb') as pdf_file:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=pdf_file,
                filename=os.path.basename(pdf_filename),
                caption=f"Execution report for {program_title}"
            )

    except Exception as e:
        logger.exception(f"Failed to generate PDF with wkhtmltopdf: {str(e)}")
        await update.message.reply_text(f"Failed to generate PDF with primary method. Trying alternative...")
        
        # Try alternative PDF generation
        await generate_and_send_pdf_alternative(update, context)
    finally:
        # Delay cleanup to ensure file is sent
        await asyncio.sleep(3)
        await cleanup(context, preserve_pdf=True)

async def generate_and_send_pdf_alternative(update: Update, context: CallbackContext):
    """Alternative PDF generation using ReportLab"""
    try:
        # Import ReportLab - make sure it's installed
        try:
            from reportlab.lib.pagesizes import letter
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Preformatted
            from reportlab.lib.styles import getSampleStyleSheet
        except ImportError:
            await update.message.reply_text("Cannot generate PDF: ReportLab library not installed. Please install it with 'pip install reportlab'.")
            return
            
        code = context.user_data['code']
        terminal_log = context.user_data['terminal_log']
        program_title = context.user_data.get('program_title', "C Program Execution Report")
        
        # Create a sanitized filename
        sanitized_title = re.sub(r'[\\/*?:"<>|]', "_", program_title)
        sanitized_title = re.sub(r'\s+', "_", sanitized_title)
        pdf_filename = os.path.abspath(f"{sanitized_title}_alt.pdf")
        
        logger.info(f"Generating alternative PDF with filename: {pdf_filename}")
        
        # Create the PDF
        doc = SimpleDocTemplate(pdf_filename, pagesize=letter)
        styles = getSampleStyleSheet()
        
        # Create content elements
        elements = []
        
        # Add title
        elements.append(Paragraph(program_title, styles['Title']))
        elements.append(Spacer(1, 12))
        
        # Add code
        elements.append(Paragraph("Source Code:", styles['Heading2']))
        elements.append(Preformatted(code, styles['Code']))
        elements.append(Spacer(1, 12))
        
        # Add terminal output
        elements.append(Paragraph("Output:", styles['Heading2']))
        terminal_output = ''.join(terminal_log)
        elements.append(Preformatted(terminal_output
