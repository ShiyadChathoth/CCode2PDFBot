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

# [Previous code remains the same until the generate_and_send_pdf function]

async def generate_and_send_pdf(update: Update, context: CallbackContext):
    try:
        code = context.user_data['code']
        execution_log = context.user_data['execution_log']
        terminal_log = context.user_data['terminal_log']
        program_title = context.user_data.get('program_title', "C Program Execution Report")

        # Generate HTML with strict page layout control
        html_content = f"""
        <html>
        <head>
            <style>
                @page {{
                    size: A4;
                    margin: 15mm;
                }}
                body {{ 
                    font-family: Arial, sans-serif; 
                    margin: 0;
                    padding: 0;
                }}
                .page {{
                    page-break-after: always;
                    height: 100vh;
                    overflow: hidden;
                    position: relative;
                }}
                .page:last-child {{
                    page-break-after: auto;
                }}
                .program-title {{
                    font-size: 30px;
                    font-weight: bold;
                    text-align: center;
                    margin-bottom: 20px;
                    text-decoration: underline;
                    text-decoration-thickness: 5px;
                    border-bottom: 3px solid black;
                    padding-bottom: 10px;
                }}
                .code-section {{
                    height: calc(100% - 100px);
                    overflow: auto;
                    margin-bottom: 20px;
                }}
                pre {{
                    font-family: 'Courier New', monospace;
                    white-space: pre-wrap;
                    font-size: 16px;
                    line-height: 1.4;
                    tab-size: 4;
                    -moz-tab-size: 4;
                    -o-tab-size: 4;
                    background: #FFFFFF;
                    padding: 5px;
                    border-radius: 3px;
                    overflow: visible;
                    margin: 0;
                }}
                .output-section {{
                    height: calc(100% - 100px);
                    overflow: auto;
                }}
                .terminal-output {{
                    font-family: 'Courier New', monospace;
                    white-space: pre;
                    font-size: 16px;
                    line-height: 1.4;
                    background: #FFFFFF;
                    padding: 10px;
                    border-radius: 3px;
                    overflow: visible;
                }}
            </style>
        </head>
        <body>
            <!-- First page - Code section -->
            <div class="page">
                <div class="program-title">{html.escape(program_title)}</div>
                <div class="code-section">
                    <pre><code>{html.escape(code)}</code></pre>
                </div>
            </div>
            
            <!-- Second page - Output section -->
            <div class="page">
                <h2>Program Output</h2>
                <div class="output-section">
                    {reconstruct_terminal_view(context)}
                </div>
            </div>
        </body>
        </html>
        """

        with open("output.html", "w") as file:
            file.write(html_content)

        # Generate sanitized filename from title
        sanitized_title = re.sub(r'[\\/*?:"<>|]', "_", program_title)
        sanitized_title = re.sub(r'\s+', "_", sanitized_title)
        pdf_filename = f"{sanitized_title}.pdf"
        
        # Generate PDF with strict page control
        subprocess.run([
            "wkhtmltopdf",
            "--enable-smart-shrinking",
            "--margin-top", "15mm",
            "--margin-bottom", "15mm",
            "--margin-left", "15mm",
            "--margin-right", "15mm",
            "--page-size", "A4",
            "--print-media-type",
            "--disable-external-links",
            "--disable-internal-links",
            "--no-background",
            "--disable-javascript",
            "--load-error-handling", "ignore",
            "--load-media-error-handling", "ignore",
            "--zoom", "1.0",
            "--footer-center", "[page]/[topage]",
            "--footer-font-size", "8",
            "--footer-spacing", "5",
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

    except Exception as e:
        await update.message.reply_text(f"Failed to generate PDF: {str(e)}")
    finally:
        await cleanup(context)

def reconstruct_terminal_view(context):
    """Preserve exact terminal formatting with standardized tabs for C programs"""
    terminal_log = context.user_data.get('terminal_log', [])
    
    if terminal_log:
        raw_output = ''.join(terminal_log)
        # Use standard C tab width (4 spaces per tab)
        raw_output = raw_output.expandtabs(4) 
        
        # Format for PDF output
        return f"""
        <div class="terminal-output">{html.escape(raw_output)}</div>
        """
    
    return "<pre>No terminal output available</pre>"

# [Rest of the code remains the same]

if __name__ == '__main__':
    main()
