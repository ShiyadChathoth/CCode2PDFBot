from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackContext,
    ConversationHandler,
    CallbackQueryHandler
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
    """Start the conversation and ask user for C code."""
    keyboard = [
        [InlineKeyboardButton("Cancel", callback_data='cancel')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        'Hi! Send me your C code, and I will compile and execute it step-by-step.',
        reply_markup=reply_markup
    )
    return CODE

async def help_command(update: Update, context: CallbackContext) -> None:
    """Send a message when the command /help is issued."""
    help_text = (
        "I can compile and run your C programs. Here's how to use me:\n\n"
        "1. Use /start to begin a new program session\n"
        "2. Send your C code as a message\n"
        "3. I'll compile and run it, showing you the output step by step\n"
        "4. Provide input when prompted\n"
        "5. Type 'done' to end program execution early\n"
        "6. Use /cancel at any time to stop the current session\n\n"
        "After your program finishes, I'll ask you for a title and generate a PDF report."
    )
    await update.message.reply_text(help_text)

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
    context.user_data['last_output_lines'] = []  # Store recent output lines for better prompt detection
    
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
            
            # Add cancel button
            keyboard = [
                [InlineKeyboardButton("Cancel Execution", callback_data='cancel_execution')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text("Code compiled successfully! Running now...", reply_markup=reply_markup)
            
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
                    
                    # Add cancel button
                    keyboard = [
                        [InlineKeyboardButton("Cancel Execution", callback_data='cancel_execution')]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await update.message.reply_text("Code compiled successfully after fixing whitespace issues! Running now...", reply_markup=reply_markup)
                    
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

async def read_process_output(update: Update, context: CallbackContext):
    process = context.user_data['process']
    output = context.user_data['output']
    errors = context.user_data['errors']
    execution_log = context.user_data['execution_log']
    output_buffer = context.user_data['output_buffer']
    terminal_log = context.user_data['terminal_log']
    last_output_lines = context.user_data.get('last_output_lines', [])
    
    output_seen = False
    read_size = 1024
    
    while True:
        stdout_task = asyncio.create_task(process.stdout.read(read_size))
        stderr_task = asyncio.create_task(process.stderr.read(read_size))
        
        done, pending = await asyncio.wait(
            [stdout_task, stderr_task],
            return_when=asyncio.FIRST_COMPLETED,
            timeout=0.5
        )

        if not done:
            for task in pending:
                task.cancel()
            
            if process.returncode is not None:
                if output_buffer:
                    process_output_chunk(context, output_buffer, update)
                    output_buffer = ""
                    context.user_data['output_buffer'] = ""
                
                if output_seen:
                    execution_log.append({
                        'type': 'system',
                        'message': 'Program execution completed.',
                        'timestamp': datetime.datetime.now()
                    })
                    
                    context.user_data['program_completed'] = True
                    
                    # Add skip button for title input
                    keyboard = [
                        [InlineKeyboardButton("Skip (Use Default Title)", callback_data='skip_title')]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await update.message.reply_text("Program execution completed.")
                    await update.message.reply_text("Please provide a title for your program (or type 'skip' to use default):", reply_markup=reply_markup)
                    return TITLE_INPUT
                else:
                    await asyncio.sleep(0.1)
                    continue
            
            if output_seen and not context.user_data.get('waiting_for_input', False):
                if output_buffer:
                    process_output_chunk(context, output_buffer, update)
                    output_buffer = ""
                    context.user_data['output_buffer'] = ""
                
                context.user_data['waiting_for_input'] = True
                
                # Get the actual prompt from the most recent output
                actual_prompt = get_actual_prompt(context)
                
                # Add done button for input
                keyboard = [
                    [InlineKeyboardButton("Done (End Program)", callback_data='done_input')]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                input_message = f"Program is waiting for input: \"{actual_prompt}\"\nPlease provide input (or type 'done' to finish):"
                
                execution_log.append({
                    'type': 'system',
                    'message': input_message,
                    'timestamp': datetime.datetime.now()
                })
                await update.message.reply_text(input_message, reply_markup=reply_markup)
            
            continue

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

        if process.returncode is not None:
            if output_buffer:
                process_output_chunk(context, output_buffer, update)
            
            execution_log.append({
                'type': 'system',
                'message': 'Program execution completed.',
                'timestamp': datetime.datetime.now()
            })
            
            context.user_data['program_completed'] = True
            
            # Add skip button for title input
            keyboard = [
                [InlineKeyboardButton("Skip (Use Default Title)", callback_data='skip_title')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text("Program execution completed.")
            await update.message.reply_text("Please provide a title for your program (or type 'skip' to use default):", reply_markup=reply_markup)
            return TITLE_INPUT

def get_actual_prompt(context):
    """Get the actual prompt from the most recent output lines."""
    last_output_lines = context.user_data.get('last_output_lines', [])
    
    # Default prompt if nothing better is found
    default_prompt = "Please enter your input"
    
    if not last_output_lines:
        return default_prompt
    
    # First check the most recent line as it's most likely to be the prompt
    last_line = last_output_lines[-1]
    if is_prompt_line(last_line):
        return last_line
    
    # If the last line doesn't look like a prompt, check the last few lines
    for line in reversed(last_output_lines):
        if is_prompt_line(line):
            return line
    
    # If we still don't have a prompt, check the execution log
    execution_log = context.user_data['execution_log']
    for entry in reversed(execution_log):
        if entry['type'] == 'prompt':
            return entry['message']
    
    # If all else fails, return the last output line anyway
    # It's better than "unknown" even if it's not a perfect prompt
    return last_output_lines[-1] if last_output_lines else default_prompt

def is_prompt_line(line):
    """Check if a line looks like a prompt."""
    # Common patterns for prompts
    return (line.rstrip().endswith((':','>','?')) or
            re.search(r'(Enter|Input|Type|Provide|Give|Please)(\s|\w)*', line, re.IGNORECASE) or
            "number" in line.lower() or
            "value" in line.lower() or
            "name" in line.lower())

def process_output_chunk(context, buffer, update):
    """Process the output buffer, preserving tabs and whitespace."""
    execution_log = context.user_data['execution_log']
    output = context.user_data['output']
    last_output_lines = context.user_data.get('last_output_lines', [])
    
    lines = re.findall(r'[^\n]*\n|[^\n]+$', buffer)
    
    new_buffer = ""
    if lines and not buffer.endswith('\n'):
        new_buffer = lines[-1]
        lines = lines[:-1]
    
    for line in lines:
        line_stripped = line.strip()
        if line_stripped:
            output.append(line_stripped)
            
            # Store recent output lines for better prompt detection
            last_output_lines.append(line_stripped)
            # Keep only the last 10 lines
            if len(last_output_lines) > 10:
                last_output_lines = last_output_lines[-10:]
            context.user_data['last_output_lines'] = last_output_lines
            
            # Enhanced prompt detection logic
            is_prompt = is_prompt_line(line_stripped)
            
            log_entry = {
                'type': 'prompt' if is_prompt else 'output',
                'message': line_stripped,
                'timestamp': datetime.datetime.now(),
                'raw': line
            }
            
            execution_log.append(log_entry)
            
            prefix = "Program prompt:" if is_prompt else "Program output:"
            asyncio.create_task(update.message.reply_text(f"{prefix} {line_stripped}"))
    
    return new_buffer

async def handle_running(update: Update, context: CallbackContext) -> int:
    user_input = update.message.text
    process = context.user_data.get('process')
    execution_log = context.user_data['execution_log']
    terminal_log = context.user_data['terminal_log']

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
        await process.stdin.drain()
        process.stdin.close()
        await process.wait()
        
        context.user_data['program_completed'] = True
        
        # Add skip button for title input
        keyboard = [
            [InlineKeyboardButton("Skip (Use Default Title)", callback_data='skip_title')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text("Please provide a title for your program (or type 'skip' to use default):", reply_markup=reply_markup)
        return TITLE_INPUT
    
    execution_log.append({
        'type': 'input',
        'message': user_input,
        'timestamp': datetime.datetime.now()
    })
    
    terminal_log.append(user_input + "\n")
    
    process.stdin.write((user_input + "\n").encode())
    await process.stdin.drain()
    context.user_data['inputs'].append(user_input)
    context.user_data['waiting_for_input'] = False
    
    await update.message.reply_text(f"Input sent: {user_input}")
    
    return RUNNING

async def handle_title_input(update: Update, context: CallbackContext) -> int:
    title = update.message.text
    
    if title.lower() == 'skip':
        context.user_data['program_title'] = "C Program Execution Report"
    else:
        context.user_data['program_title'] = title
    
    await update.message.reply_text(f"Using title: {context.user_data['program_title']}")
    await generate_and_send_pdf(update, context)
    return ConversationHandler.END

async def button_callback(update: Update, context: CallbackContext) -> int:
    """Handle button callbacks."""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'cancel':
        await query.edit_message_text(text="Operation cancelled.")
        return ConversationHandler.END
    
    elif query.data == 'cancel_execution':
        process = context.user_data.get('process')
        if process:
            try:
                process.kill()
                await query.edit_message_text(text="Program execution cancelled.")
            except:
                await query.edit_message_text(text="Failed to cancel program execution.")
        else:
            await query.edit_message_text(text="No program is currently running.")
        return ConversationHandler.END
    
    elif query.data == 'done_input':
        process = context.user_data.get('process')
        execution_log = context.user_data['execution_log']
        
        if process:
            execution_log.append({
                'type': 'system',
                'message': 'User terminated the program.',
                'timestamp': datetime.datetime.now()
            })
            await process.stdin.drain()
            process.stdin.close()
            await process.wait()
            
            context.user_data['program_completed'] = True
            
            # Add skip button for title input
            keyboard = [
                [InlineKeyboardButton("Skip (Use Default Title)", callback_data='skip_title')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(text="Program terminated by user.")
            await update.effective_chat.send_message("Please provide a title for your program (or type 'skip' to use default):", reply_markup=reply_markup)
            return TITLE_INPUT
    
    elif query.data == 'skip_title':
        context.user_data['program_title'] = "C Program Execution Report"
        await query.edit_message_text(text=f"Using title: {context.user_data['program_title']}")
        await generate_and_send_pdf(update, context)
        return ConversationHandler.END
    
    return RUNNING

async def generate_and_send_pdf(update: Update, context: CallbackContext):
    try:
        code = context.user_data['code']
        execution_log = context.user_data['execution_log']
        terminal_log = context.user_data['terminal_log']
        program_title = context.user_data.get('program_title', "C Program Execution Report")

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

        # Generate sanitized filename from title
        # Replace invalid filename characters with underscores and ensure it ends with .pdf
        sanitized_title = re.sub(r'[\\/*?:"<>|]', "_", program_title)
        sanitized_title = re.sub(r'\s+', "_", sanitized_title)  # Replace spaces with underscores
        pdf_filename = f"{sanitized_title}.pdf"
        
        # Rest of the function implementation...
        
    except Exception as e:
        await update.effective_chat.send_message(f"Error generating PDF: {str(e)}")

def reconstruct_terminal_view(context):
    """Reconstruct terminal view from execution log."""
    execution_log = context.user_data['execution_log']
    terminal_html = "<pre style='background-color: #f0f0f0; padding: 10px; border-radius: 5px;'>"
    
    for entry in execution_log:
        if entry['type'] == 'system':
            if 'Program execution completed' in entry['message']:
                terminal_html += f"<span style='color: green;'>{html.escape(entry['message'])}</span>\n"
            else:
                terminal_html += f"<span style='color: blue;'>{html.escape(entry['message'])}</span>\n"
        elif entry['type'] == 'error':
            terminal_html += f"<span style='color: red;'>{html.escape(entry['message'])}</span>\n"
        elif entry['type'] == 'prompt':
            terminal_html += f"<span style='color: purple;'>{html.escape(entry['message'])}</span>\n"
        elif entry['type'] == 'input':
            terminal_html += f"<span style='color: green;'>Input: {html.escape(entry['message'])}</span>\n"
        elif entry['type'] == 'output':
            terminal_html += f"{html.escape(entry['message'])}\n"
    
    terminal_html += "</pre>"
    return terminal_html

async def cancel(update: Update, context: CallbackContext) -> int:
    """Cancel and end the conversation."""
    process = context.user_data.get('process')
    if process:
        try:
            process.kill()
        except:
            pass
    
    await update.message.reply_text('Operation cancelled.')
    return ConversationHandler.END

def main():
    application = Application.builder().token(TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_code)],
            RUNNING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_running),
                CallbackQueryHandler(button_callback)
            ],
            TITLE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_title_input),
                CallbackQueryHandler(button_callback)
            ]
        },
        fallbacks=[
            CommandHandler('cancel', cancel),
            CommandHandler('start', start)
        ]
    )
    
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler('help', help_command))
    
    application.run_polling()

if __name__ == '__main__':
    main()
