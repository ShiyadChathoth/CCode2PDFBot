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
import datetime
import unicodedata
from aiohttp import web

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Environment Variables ---
# Telegram Bot Token
TOKEN = os.getenv('TOKEN')
# Port for the web server, Render sets this automatically
PORT = int(os.getenv('PORT', 8080))


if not TOKEN:
    raise ValueError("No TOKEN provided in environment variables!")

# States for ConversationHandler
CODE, RUNNING, ASK_TITLE = range(3)

# --- Web Server for Keep-Alive ---

async def health_check(request):
    """A simple health check endpoint for Render to ping."""
    logger.info("Health check endpoint was pinged.")
    return web.Response(text="I'm alive!")

# --- Bot Logic (Your existing functions) ---

async def start(update: Update, context: CallbackContext) -> int:
    """Starts the conversation and asks for C code."""
    await update.message.reply_text(
        'Hi! Send me your C code, and I will compile and execute it step-by-step.'
    )
    return CODE

def clean_whitespace(code: str) -> str:
    """Cleans non-standard whitespace characters from the code."""
    cleaned_code = code.replace('\u00A0', ' ')
    for char in code:
        if unicodedata.category(char).startswith('Z') and char != ' ':
            cleaned_code = cleaned_code.replace(char, ' ')
    cleaned_code = cleaned_code.replace('\t', '    ')
    return cleaned_code

async def handle_code(update: Update, context: CallbackContext) -> int:
    """Receives, cleans, and compiles the C code."""
    original_code = update.message.text
    code = clean_whitespace(original_code)

    if code != original_code:
        await update.message.reply_text(
            "⚠️ I detected and fixed non-standard whitespace characters in your code."
        )

    context.user_data.update({
        'code': code, 'output': [], 'inputs': [], 'errors': [],
        'execution_log': [], 'terminal_log': []
    })

    try:
        with open("temp.c", "w", encoding='utf-8') as file:
            file.write(code)

        compile_result = subprocess.run(
            ["gcc", "temp.c", "-o", "temp"],
            capture_output=True, text=True, check=False
        )

        if compile_result.returncode == 0:
            context.user_data['execution_log'].append({
                'type': 'system', 'message': 'Code compiled successfully!',
                'timestamp': datetime.datetime.now()
            })
            process = await asyncio.create_subprocess_exec(
                "stdbuf", "-o0", "./temp",
                stdin=PIPE, stdout=PIPE, stderr=PIPE
            )
            context.user_data['process'] = process
            await update.message.reply_text("Code compiled successfully! Running now...")
            return await handle_running_logic(update, context)
        else:
            error_message = compile_result.stderr
            await update.message.reply_text(f"Compilation Error:\n<pre>{html.escape(error_message)}</pre>", parse_mode='HTML')
            return ConversationHandler.END

    except Exception as e:
        await update.message.reply_text(f"An error occurred: {e}")
        return ConversationHandler.END

async def read_from_stream(stream):
    """Reads from a stream without blocking indefinitely."""
    buffer = b''
    while True:
        try:
            chunk = await asyncio.wait_for(stream.read(1024), timeout=0.1)
            if not chunk:
                break
            buffer += chunk
        except asyncio.TimeoutError:
            break
    return buffer.decode('utf-8', errors='ignore')

async def handle_running_logic(update: Update, context: CallbackContext):
    """Manages the I/O loop for the running C program."""
    process = context.user_data['process']

    output = await read_from_stream(process.stdout)
    if output:
        await update.message.reply_text(f"Program output:\n{output}")
        context.user_data['terminal_log'].append({'type': 'output', 'content': output, 'timestamp': datetime.datetime.now()})

    if process.returncode is not None:
        await update.message.reply_text("Program execution completed.")
        return await ask_for_title(update, context)

    await update.message.reply_text("Please provide input, or type 'done' to finish.")
    return RUNNING

async def handle_running(update: Update, context: CallbackContext) -> int:
    """Handles user input while the C program is running."""
    user_input = update.message.text
    process = context.user_data.get('process')

    if not process or process.returncode is not None:
        await update.message.reply_text("The program is no longer running.")
        return await ask_for_title(update, context)


    if user_input.lower() == 'done':
        if process.stdin and not process.stdin.is_closing():
            process.stdin.close()
        await process.wait()
        await update.message.reply_text("Program terminated by user.")
        return await ask_for_title(update, context)

    context.user_data['terminal_log'].append({
        'type': 'input', 'content': user_input + "\n",
        'timestamp': datetime.datetime.now()
    })
    process.stdin.write((user_input + "\n").encode())
    await process.stdin.drain()
    await update.message.reply_text(f"Input sent: {user_input}")

    return await handle_running_logic(update, context)

async def ask_for_title(update: Update, context: CallbackContext) -> int:
    """Prompts the user to enter a title for the PDF report."""
    await update.message.reply_text("Please enter a title for the PDF report.")
    return ASK_TITLE

async def receive_title(update: Update, context: CallbackContext) -> int:
    """Receives the title and triggers the report generation."""
    title = update.message.text
    await generate_and_send_pdf(update, context, title)
    return ConversationHandler.END

async def generate_and_send_pdf(update: Update, context: CallbackContext, title: str = "Execution Log"):
    try:
        code = context.user_data['code']
        terminal_log = context.user_data['terminal_log']
        terminal_log.sort(key=lambda x: x['timestamp'])

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>{html.escape(title)} - C Program Execution Report</title>
            <style>
                body {{ font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; margin: 25px; background-color: #f4f4f9; color: #333; }}
                h1 {{ color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 10px; }}
                h2 {{ color: #3498db; margin-top: 30px; }}
                pre {{ background-color: #ffffff; padding: 15px; border: 1px solid #ddd; border-radius: 5px; overflow-x: auto; white-space: pre-wrap; word-wrap: break-word; }}
                code {{ font-family: 'Courier New', Courier, monospace; }}
                .terminal {{ background-color: #2b2b2b; color: #f8f8f2; padding: 20px; border-radius: 5px; font-family: monospace; white-space: pre-wrap; line-height: 1.5; }}
            </style>
        </head>
        <body>
            <h1>{html.escape(title)}</h1>
            <h2>Source Code</h2>
            <pre><code>{html.escape(code)}</code></pre>
            <h2>Terminal Output</h2>
            <div class="terminal">{''.join(html.escape(entry['content']) for entry in terminal_log)}</div>
        </body>
        </html>
        """

        with open("output.html", "w", encoding='utf-8') as file:
            file.write(html_content)

        pdf_process = subprocess.run(
            ["wkhtmltopdf", "--enable-local-file-access", "output.html", "output.pdf"],
            capture_output=True, text=True, check=False
        )

        if pdf_process.returncode != 0:
            logger.error(f"PDF generation failed: {pdf_process.stderr}")
            await update.message.reply_text("Failed to generate PDF report. Sending HTML instead.")
            with open('output.html', 'rb') as html_file:
                await context.bot.send_document(chat_id=update.effective_chat.id, document=html_file, filename="program_execution.html")
        else:
            await update.message.reply_text("Generating execution report...")
            pdf_filename = f"{title.replace(' ', '_').lower()}.pdf"
            with open('output.pdf', 'rb') as pdf_file:
                await context.bot.send_document(chat_id=update.effective_chat.id, document=pdf_file, filename=pdf_filename)

    except Exception as e:
        logger.error(f"Error in PDF/HTML generation: {str(e)}")
        await update.message.reply_text(f"Failed to generate report: {str(e)}")
    finally:
        await cleanup(context)

async def cleanup(context: CallbackContext):
    """Cleans up temporary files and clears user data."""
    process = context.user_data.get('process')
    if process and process.returncode is None:
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            if process.returncode is None:
                process.kill()

    for file in ["temp.c", "temp", "output.pdf", "output.html"]:
        if os.path.exists(file):
            os.remove(file)
    context.user_data.clear()

async def cancel(update: Update, context: CallbackContext) -> int:
    """Cancels the current operation."""
    await update.message.reply_text("Operation cancelled.")
    await cleanup(context)
    return ConversationHandler.END

# --- Main Application Runner ---

def main() -> None:
    """Initializes and runs the bot and web server concurrently."""
    # Create the Telegram bot application
    bot_app = Application.builder().token(TOKEN).build()

    # Add the conversation handler to the bot
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_code)],
            RUNNING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_running)],
            ASK_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    bot_app.add_handler(conv_handler)

    # Create the aiohttp web application
    web_app = web.Application()

    # Define startup and shutdown web server handlers
    async def on_startup(app):
        """Starts the bot when the web server starts."""
        logger.info("Starting bot polling...")
        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_polling()

    async def on_shutdown(app):
        """Stops the bot when the web server shuts down."""
        logger.info("Stopping bot...")
        await bot_app.updater.stop()
        await bot_app.stop()

    # Register the handlers
    web_app.on_startup.append(on_startup)
    web_app.on_shutdown.append(on_shutdown)
    web_app.router.add_get("/health", health_check)

    # Run the web server
    # This will automatically start the bot on startup and stop it on shutdown
    logger.info(f"Starting web server on port {PORT}")
    web.run_app(web_app, host="0.0.0.0", port=PORT)


if __name__ == '__main__':
    # Before running, make sure you have aiohttp installed:
    # pip install aiohttp
    main()
