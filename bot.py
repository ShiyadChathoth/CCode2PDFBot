import asyncio
import datetime
import html
import logging
import os
import re
import shutil
import unicodedata
from asyncio.subprocess import PIPE

import telegram
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    CallbackContext,
)

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Environment and Configuration ---
TOKEN = os.getenv('TOKEN')
if not TOKEN:
    raise ValueError("No TOKEN provided in environment variables!")

# States for ConversationHandler
CODE, RUNNING = range(2)


# --- Helper Functions ---

def clean_whitespace(code: str) -> str:
    """Clean non-standard whitespace characters from code."""
    cleaned_code = code.replace('\u00A0', ' ')
    for char in code:
        if unicodedata.category(char).startswith('Z') and char != ' ':
            cleaned_code = cleaned_code.replace(char, ' ')
    cleaned_code = cleaned_code.replace('\t', '    ')
    return cleaned_code


async def check_dependencies():
    """Check if required system dependencies (gcc, wkhtmltopdf) are installed."""
    if not shutil.which("gcc"):
        logger.error("FATAL: 'gcc' is not installed or not in PATH. The bot cannot compile C code.")
        raise RuntimeError("'gcc' is not installed. Please install it to run this bot.")
    if not shutil.which("wkhtmltopdf"):
        logger.warning("WARNING: 'wkhtmltopdf' is not installed. PDF generation will be disabled.")
    logger.info("All required dependencies are available.")


# --- Core Bot Logic ---

async def start(update: Update, context: CallbackContext) -> int:
    """Handles the /start command."""
    await update.message.reply_text(
        'Hi! Send me your C code, and I will compile and execute it for you. '
        'Type /cancel at any time to stop.'
    )
    return CODE


async def handle_code(update: Update, context: CallbackContext) -> int:
    """Receives C code, compiles it, and prepares for execution."""
    original_code = update.message.text
    code = clean_whitespace(original_code)

    if code != original_code:
        await update.message.reply_text(
            "⚠️ I detected and fixed non-standard whitespace characters in your code."
        )

    context.user_data.clear()
    context.user_data['code'] = code
    context.user_data['execution_log'] = []
    context.user_data['terminal_log'] = []
    context.user_data['output_buffer'] = ""

    try:
        with open("temp.c", "w", encoding="utf-8") as file:
            file.write(code)

        compile_process = await asyncio.create_subprocess_exec(
            "gcc", "temp.c", "-o", "temp",
            stdout=PIPE,
            stderr=PIPE
        )
        _, compile_stderr = await compile_process.communicate()

        if compile_process.returncode == 0:
            context.user_data['execution_log'].append({
                'type': 'system',
                'message': 'Code compiled successfully!',
                'timestamp': datetime.datetime.now()
            })

            process = await asyncio.create_subprocess_exec(
                "stdbuf", "-o0", "./temp",
                stdin=PIPE, stdout=PIPE, stderr=PIPE
            )
            context.user_data['process'] = process

            await update.message.reply_text("✅ Code compiled successfully! Running now...")
            asyncio.create_task(read_process_output(update, context))
            return RUNNING
        else:
            error_message = compile_stderr.decode()
            context.user_data['execution_log'].append({
                'type': 'error',
                'message': f"Compilation Error:\n{error_message}",
                'timestamp': datetime.datetime.now()
            })
            if "stray" in error_message and ("\\302" in error_message or "\\240" in error_message):
                await update.message.reply_text(
                    f"❌ Compilation Error (non-standard whitespace):\n{html.escape(error_message)}\n\n"
                    "This often happens when copying code from websites or documents. Please try retyping the code in a plain text editor."
                )
            else:
                await update.message.reply_text(f"❌ Compilation Error:\n{html.escape(error_message)}")
            
            await cleanup(context)
            return ConversationHandler.END

    except Exception as e:
        logger.error(f"An error occurred in handle_code: {e}")
        await update.message.reply_text(f"An unexpected error occurred: {e}")
        await cleanup(context)
        return ConversationHandler.END


async def read_process_output(update: Update, context: CallbackContext):
    """Reads and processes the output from the running C program."""
    process = context.user_data.get('process')
    if not process:
        return

    output_buffer = context.user_data.get('output_buffer', "")
    
    try:
        while process.returncode is None:
            tasks = {
                asyncio.create_task(process.stdout.read(1024), name="stdout"),
                asyncio.create_task(process.stderr.read(1024), name="stderr")
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED, timeout=0.2)

            for task in pending:
                task.cancel()

            if not done:
                if output_buffer and not context.user_data.get('waiting_for_input'):
                    context.user_data['waiting_for_input'] = True
                    await update.message.reply_text("▶️ Program seems to be waiting for input. Please provide it, or type /cancel to stop.")
                continue

            for task in done:
                if task.get_name() == "stdout":
                    chunk = task.result()
                    if chunk:
                        output_buffer += chunk.decode()
                        output_buffer = process_output_chunk(context, output_buffer, update)
                elif task.get_name() == "stderr":
                    chunk = task.result()
                    if chunk:
                        error_message = chunk.decode().strip()
                        context.user_data['execution_log'].append({'type': 'error', 'message': error_message, 'timestamp': datetime.datetime.now()})
                        await update.message.reply_text(f"Runtime Error: {html.escape(error_message)}")
            
            context.user_data['output_buffer'] = output_buffer

    except Exception as e:
        logger.error(f"Error reading process output: {e}")
    finally:
        if process.returncode is not None:
            if output_buffer:
                process_output_chunk(context, output_buffer, update)
            
            context.user_data['execution_log'].append({'type': 'system', 'message': 'Program execution completed.', 'timestamp': datetime.datetime.now()})
            await update.message.reply_text("✅ Program execution completed.")
            await generate_and_send_pdf(update, context)


def process_output_chunk(context: CallbackContext, buffer: str, update: Update) -> str:
    """Processes the output buffer, sends complete lines to the user, and returns the remainder."""
    lines = buffer.split('\n')
    if len(lines) == 1:
        return buffer

    complete_lines = lines[:-1]
    new_buffer = lines[-1]

    for line in complete_lines:
        if not line:
            continue
        
        is_prompt = line.rstrip().endswith((':','>','?')) or re.search(r'(enter|input|type)', line, re.IGNORECASE)
        log_type = 'prompt' if is_prompt else 'output'
        
        context.user_data['execution_log'].append({'type': log_type, 'message': line, 'timestamp': datetime.datetime.now()})
        context.user_data['terminal_log'].append({'type': 'output', 'content': line + '\n', 'timestamp': datetime.datetime.now()})
        
        prefix = "💬 Program prompt:" if is_prompt else "📄 Program output:"
        asyncio.create_task(update.message.reply_text(f"{prefix}\n<pre>{html.escape(line)}</pre>", parse_mode='HTML'))

    return new_buffer


async def handle_running(update: Update, context: CallbackContext) -> int:
    """Handles user input while the C program is running."""
    user_input = update.message.text
    process = context.user_data.get('process')

    if not process or process.returncode is not None:
        await update.message.reply_text("Program is not running anymore.")
        await cleanup(context)
        return ConversationHandler.END

    context.user_data['execution_log'].append({'type': 'input', 'message': user_input, 'timestamp': datetime.datetime.now()})
    context.user_data['terminal_log'].append({'type': 'input', 'content': user_input + '\n', 'timestamp': datetime.datetime.now()})
    
    try:
        process.stdin.write((user_input + "\n").encode())
        await process.stdin.drain()
        context.user_data['waiting_for_input'] = False
    except (BrokenPipeError, ConnectionResetError):
        await update.message.reply_text("The program terminated before it could receive your input.")
        await cleanup(context)
        return ConversationHandler.END

    return RUNNING


async def generate_and_send_pdf(update: Update, context: CallbackContext):
    """Generates and sends an HTML and PDF report of the execution."""
    if not shutil.which("wkhtmltopdf"):
        await update.message.reply_text("`wkhtmltopdf` is not installed, so I can only send an HTML report.")
        await send_html_report(update, context)
        await cleanup(context)
        return

    try:
        html_content = generate_html_report(context)
        with open("output.html", "w", encoding="utf-8") as file:
            file.write(html_content)

        pdf_process = await asyncio.create_subprocess_exec(
            "wkhtmltopdf", "--enable-local-file-access", "output.html", "output.pdf",
            stderr=PIPE
        )
        _, pdf_stderr = await pdf_process.communicate()

        if pdf_process.returncode != 0:
            logger.error(f"PDF generation failed: {pdf_stderr.decode()}")
            await update.message.reply_text("Failed to generate PDF report. Sending HTML instead.")
            await send_html_report(update, context)
        else:
            await update.message.reply_text("📊 Execution report generated. Sending PDF and HTML...")
            with open('output.pdf', 'rb') as pdf_file:
                await context.bot.send_document(chat_id=update.effective_chat.id, document=pdf_file, filename="execution_report.pdf")
            await send_html_report(update, context)

    except Exception as e:
        logger.error(f"Error in PDF generation: {e}")
        await update.message.reply_text(f"Failed to generate report: {e}")
    finally:
        await cleanup(context)


def generate_html_report(context: CallbackContext) -> str:
    """Generates the HTML content for the report."""
    code = context.user_data.get('code', 'No code found.')
    terminal_log = sorted(context.user_data.get('terminal_log', []), key=lambda x: x['timestamp'])

    terminal_content = ""
    for entry in terminal_log:
        content = html.escape(entry['content'])
        if entry['type'] == 'input':
            terminal_content += f'<span class="input">{content}</span>'
        else:
            terminal_content += f'<span class="output">{content}</span>'

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>C Program Execution Report</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 20px; background-color: #fdfdfd; }}
            h1, h2 {{ color: #2c3e50; border-bottom: 1px solid #eee; padding-bottom: 10px; }}
            pre {{ background-color: #2b2b2b; color: #f8f8f2; padding: 15px; border-radius: 5px; overflow-x: auto; white-space: pre-wrap; }}
            .terminal {{ line-height: 1.5; }}
            .terminal .input {{ color: #a9f971; font-weight: bold;}}
            .terminal .output {{ color: #f8f8f2; }}
        </style>
    </head>
    <body>
        <h1>C Program Execution Report</h1>
        <h2>Source Code</h2>
        <pre><code>{html.escape(code)}</code></pre>
        <h2>Terminal View</h2>
        <pre class="terminal">{terminal_content}</pre>
    </body>
    </html>
    """

async def send_html_report(update: Update, context: CallbackContext):
    """Sends the HTML report."""
    try:
        with open('output.html', 'rb') as html_file:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=html_file,
                filename="execution_report.html"
            )
    except FileNotFoundError:
        logger.error("output.html not found for sending.")
        await update.message.reply_text("Could not generate the HTML report file.")


async def cleanup(context: CallbackContext):
    """Cleans up processes and temporary files."""
    process = context.user_data.get('process')
    if process and process.returncode is None:
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            process.kill()
        except Exception as e:
            logger.error(f"Error during process cleanup: {e}")

    for file in ["temp.c", "temp", "output.pdf", "output.html"]:
        if os.path.exists(file):
            try:
                os.remove(file)
            except OSError as e:
                logger.error(f"Error removing file {file}: {e}")
    
    context.user_data.clear()


async def cancel(update: Update, context: CallbackContext) -> int:
    """Cancels the current operation."""
    await update.message.reply_text("Operation cancelled.")
    await cleanup(context)
    return ConversationHandler.END


async def post_init(application: Application):
    """A function to run after the application is initialized."""
    await check_dependencies()
    logger.info("Bot is ready and polling...")


def main() -> None:
    """Main function to set up and run the bot."""
    application = Application.builder().token(TOKEN).post_init(post_init).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_code)],
            RUNNING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_running)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        conversation_timeout=datetime.timedelta(minutes=10).total_seconds()
    )

    application.add_handler(conv_handler)

    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except telegram.error.Conflict:
        logger.error("Conflict error: Another instance of the bot is already running.")
        print("FATAL: Could not start the bot. Another instance is already running with the same token.")
    except RuntimeError as e:
        logger.error(f"A runtime error occurred during startup: {e}")
        print(f"FATAL: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")


if __name__ == '__main__':
    main()
