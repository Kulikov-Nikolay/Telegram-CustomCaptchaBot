import asyncio
import random
import html
import json
import traceback
import logging
import datetime
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, JobQueue, CallbackQueryHandler
from telegram.error import TelegramError, BadRequest
from telegram.constants import ParseMode
from collections import defaultdict
from datetime import time as dt_time
from datetime import datetime, timedelta

import os
from dotenv import load_dotenv
import mysql.connector
from mysql.connector import Error

load_dotenv() # This reads the environment variables inside .env

# Get environment variables
DB_HOST = os.getenv('DB_HOST')
DB_PORT = int(os.getenv('DB_PORT', 3306))
DB_NAME = os.getenv('DB_NAME')
DB_USER = os.getenv('DB_USER')
DB_PASSWORD = os.getenv('DB_PASSWORD')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

logger = logging.getLogger(__name__)

# Store pending captchas: {user_id: correct_answer}
pending_captchas = {}

# Store custom captcha questions: {chat_id: (mode, question, answers)}
# mode: "open" for open-ended, "multiple" for multiple-choice
custom_captchas = {}

# Store custom timeout settings: {chat_id: timeout_seconds}
custom_timeouts = {}

# Store custom attempt limits: {chat_id: max_attempts}
custom_attempt_limits = {}

# Store user attempts: {user_id: current_attempts}
user_attempts = {}

# Add this to your global variables:
user_messages = {}

join_messages = {}

custom_welcome_messages = {}

user_attempts = {}

strict_mode = {}  # {chat_id: bool}

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

def get_db_connection():
    try:
        connection = mysql.connector.connect(
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD
        )
        return connection
    except Error as e:
        print(f"Error connecting to MySQL database: {e}")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    await update.message.reply_text('Hi! I am a captcha bot.')

async def get_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    
    connection = get_db_connection()
    if connection is None:
        await update.message.reply_text("Sorry, there was a problem connecting to the database. Please try again later.")
        return

    cursor = connection.cursor()
    try:
        cursor.execute("SELECT timeout FROM chat_settings WHERE chat_id = %s", (chat_id,))
        result = cursor.fetchone()
        
        if result:
            timeout = result[0]
        else:
            timeout = 60  # Default timeout if not set
        
        await update.message.reply_text(f"The current captcha timeout is set to {timeout} seconds.")
    except Error as e:
        print(f"Error getting timeout: {e}")
        await update.message.reply_text("Sorry, there was a problem retrieving the timeout. Please try again later.")
    finally:
        cursor.close()
        connection.close()

async def set_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message or update.edited_message
    if not message:
        return

    user = await update.effective_chat.get_member(message.from_user.id)
    if user.status not in ['creator', 'administrator']:
        await message.reply_text("Sorry, only admins can use this command.")
        return

    if len(context.args) != 1:
        await message.reply_text("Usage: /settimeout <seconds>")
        return

    try:
        timeout = int(context.args[0])
        if timeout <= 0:
            raise ValueError("Timeout must be positive")
    except ValueError:
        await message.reply_text("Please provide a valid positive number of seconds.")
        return

    chat_id = update.effective_chat.id
    
    connection = get_db_connection()
    if connection is None:
        await message.reply_text("Sorry, there was a problem connecting to the database. Please try again later.")
        return

    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO chat_settings (chat_id, timeout) VALUES (%s, %s) ON DUPLICATE KEY UPDATE timeout = %s",
            (chat_id, timeout, timeout)
        )
        connection.commit()
        await message.reply_text(f"Captcha timeout set to {timeout} seconds.")
    except Error as e:
        print(f"Error setting timeout: {e}")
        await message.reply_text("Sorry, there was a problem setting the timeout. Please try again later.")
    finally:
        cursor.close()
        connection.close()

async def set_attempt_limit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message or update.edited_message
    if not message:
        return

    user = await update.effective_chat.get_member(message.from_user.id)
    if user.status not in ['creator', 'administrator']:
        await message.reply_text("Sorry, only admins can use this command.")
        return

    if len(context.args) != 1:
        await message.reply_text("Usage: /setattemptlimit <number>")
        return

    try:
        limit = int(context.args[0])
        if limit <= 0:
            raise ValueError("Attempt limit must be positive")
    except ValueError:
        await message.reply_text("Please provide a valid positive number.")
        return

    chat_id = update.effective_chat.id
    
    connection = get_db_connection()
    if connection is None:
        await message.reply_text("Sorry, there was a problem connecting to the database. Please try again later.")
        return

    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO chat_settings (chat_id, attempt_limit) VALUES (%s, %s) ON DUPLICATE KEY UPDATE attempt_limit = %s",
            (chat_id, limit, limit)
        )
        connection.commit()
        await message.reply_text(f"Captcha attempt limit set to {limit}.")
    except Error as e:
        print(f"Error setting attempt limit: {e}")
        await message.reply_text("Sorry, there was a problem setting the attempt limit. Please try again later.")
    finally:
        cursor.close()
        connection.close()

async def get_attempt_limit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    
    connection = get_db_connection()
    if connection is None:
        await update.message.reply_text("Sorry, there was a problem connecting to the database. Please try again later.")
        return

    cursor = connection.cursor()
    try:
        cursor.execute("SELECT attempt_limit FROM chat_settings WHERE chat_id = %s", (chat_id,))
        result = cursor.fetchone()
        
        if result:
            limit = result[0]
        else:
            limit = 3  # Default attempt limit if not set
        
        await update.message.reply_text(f"The current captcha attempt limit is set to {limit}.")
    except Error as e:
        print(f"Error getting attempt limit: {e}")
        await update.message.reply_text("Sorry, there was a problem retrieving the attempt limit. Please try again later.")
    finally:
        cursor.close()
        connection.close()

async def set_welcome_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await update.effective_chat.get_member(update.effective_user.id)
    if user.status not in ['creator', 'administrator']:
        await update.message.reply_text("Sorry, only admins can use this command.")
        return

    chat_id = update.effective_chat.id
    
    if not context.args:
        await update.message.reply_text("Please provide a welcome message after the command. For example:\n/setwelcomemessage Welcome to our group!")
        return

    welcome_message = ' '.join(context.args)
    
    connection = get_db_connection()
    if connection is None:
        await update.message.reply_text("Sorry, there was a problem connecting to the database. Please try again later.")
        return

    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO chat_settings (chat_id, welcome_message) VALUES (%s, %s) ON DUPLICATE KEY UPDATE welcome_message = %s",
            (chat_id, welcome_message, welcome_message)
        )
        connection.commit()
        await update.message.reply_text(f"Welcome message has been set to:\n\n{welcome_message}")
    except Error as e:
        print(f"Error setting welcome message: {e}")
        await update.message.reply_text("Sorry, there was a problem setting the welcome message. Please try again later.")
    finally:
        cursor.close()
        connection.close()

async def get_welcome_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    
    connection = get_db_connection()
    if connection is None:
        await update.message.reply_text("Sorry, there was a problem connecting to the database. Please try again later.")
        return

    cursor = connection.cursor()
    try:
        cursor.execute("SELECT welcome_message FROM chat_settings WHERE chat_id = %s", (chat_id,))
        result = cursor.fetchone()
        
        if result and result[0]:
            welcome_message = result[0]
            await update.message.reply_text(f"The current welcome message is:\n\n{welcome_message}")
        else:
            await update.message.reply_text("No custom welcome message has been set for this chat. The default welcome message will be used.")
    except Error as e:
        print(f"Error getting welcome message: {e}")
        await update.message.reply_text("Sorry, there was a problem retrieving the welcome message. Please try again later.")
    finally:
        cursor.close()
        connection.close()

async def set_strict_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await update.effective_chat.get_member(update.effective_user.id)
    if user.status not in ['creator', 'administrator']:
        await update.message.reply_text("Sorry, only admins can use this command.")
        return

    chat_id = update.effective_chat.id
    
    connection = get_db_connection()
    if connection is None:
        await update.message.reply_text("Sorry, there was a problem connecting to the database. Please try again later.")
        return

    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO chat_settings (chat_id, strict_mode) VALUES (%s, TRUE) ON DUPLICATE KEY UPDATE strict_mode = TRUE",
            (chat_id,)
        )
        connection.commit()
        await update.message.reply_text("Strict mode enabled. Users who fail the captcha will be permanently banned.")
    except Error as e:
        print(f"Error setting strict mode: {e}")
        await update.message.reply_text("Sorry, there was a problem enabling strict mode. Please try again later.")
    finally:
        cursor.close()
        connection.close()

async def unset_strict_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await update.effective_chat.get_member(update.effective_user.id)
    if user.status not in ['creator', 'administrator']:
        await update.message.reply_text("Sorry, only admins can use this command.")
        return

    chat_id = update.effective_chat.id
    
    connection = get_db_connection()
    if connection is None:
        await update.message.reply_text("Sorry, there was a problem connecting to the database. Please try again later.")
        return

    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO chat_settings (chat_id, strict_mode) VALUES (%s, FALSE) ON DUPLICATE KEY UPDATE strict_mode = FALSE",
            (chat_id,)
        )
        connection.commit()
        await update.message.reply_text("Strict mode disabled. Users who fail the captcha will be kicked but not banned.")
    except Error as e:
        print(f"Error unsetting strict mode: {e}")
        await update.message.reply_text("Sorry, there was a problem disabling strict mode. Please try again later.")
    finally:
        cursor.close()
        connection.close()

async def get_all_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await update.effective_chat.get_member(update.effective_user.id)
    if user.status not in ['creator', 'administrator']:
        await update.message.reply_text("Sorry, only admins can use this command.")
        return

    chat_id = update.effective_chat.id
    
    connection = get_db_connection()
    if connection is None:
        await update.message.reply_text("Sorry, there was a problem connecting to the database. Please try again later.")
        return

    cursor = connection.cursor(dictionary=True)
    try:
        # Get chat settings
        cursor.execute("SELECT * FROM chat_settings WHERE chat_id = %s", (chat_id,))
        chat_settings = cursor.fetchone()
        
        if not chat_settings:
            chat_settings = {
                'timeout': 60,
                'attempt_limit': 3,
                'welcome_message': "Welcome to the group!",
                'strict_mode': False,
                'welcome_timeout': 10
            }
        
        # Get captcha settings
        cursor.execute("SELECT * FROM captchas WHERE chat_id = %s", (chat_id,))
        captcha_settings = cursor.fetchone()
        
        settings_message = f"""
Current settings for this chat:

1. Captcha timeout: {chat_settings.get('timeout', 60)} seconds
2. Attempt limit: {chat_settings.get('attempt_limit', 3)}
3. Welcome message: "{chat_settings.get('welcome_message', 'Welcome to the group!')}"
4. Welcome message timeout: {chat_settings.get('welcome_timeout', 10)} seconds
5. Strict mode: {"Enabled" if chat_settings.get('strict_mode', False) else "Disabled"}

"""

        if captcha_settings:
            settings_message += f"""
6. Captcha type: {captcha_settings['mode']}
7. Captcha question: "{captcha_settings['question']}"
8. Captcha answer(s): {captcha_settings['answers']}
"""
        else:
            settings_message += """
6. Captcha type: Default
7. Captcha question: "What is 2+2?"
8. Captcha answer(s): 4, four
"""

        await update.message.reply_text(settings_message)
    except Error as e:
        print(f"Error getting all settings: {e}")
        await update.message.reply_text("Sorry, there was a problem retrieving the settings. Please try again later.")
    finally:
        cursor.close()
        connection.close()

async def update_group_statistics(context: ContextTypes.DEFAULT_TYPE) -> None:
    bot = context.bot
    connection = get_db_connection()
    if connection is None:
        print("Failed to connect to the database")
        return

    cursor = connection.cursor()
    try:
        # Get all unique chat_ids from the chat_settings table
        cursor.execute("SELECT DISTINCT chat_id FROM chat_settings")
        chat_ids = cursor.fetchall()

        for (chat_id,) in chat_ids:
            try:
                # Get the member count for the chat
                chat_member_count = await bot.get_chat_member_count(chat_id)

                # Insert the data into the group_statistics table
                cursor.execute("""
                    INSERT INTO group_statistics (chat_id, member_count)
                    VALUES (%s, %s)
                """, (chat_id, chat_member_count))

                print(f"Updated statistics for chat {chat_id}: {chat_member_count} members")
            except TelegramError as e:
                print(f"Error getting member count for chat {chat_id}: {e}")

        connection.commit()
    except Error as e:
        print(f"Database error in update_group_statistics: {e}")
    finally:
        cursor.close()
        connection.close()

async def set_open_captcha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await update.effective_chat.get_member(update.effective_user.id)
    if user.status not in ['creator', 'administrator']:
        await update.message.reply_text("Sorry, only admins can use this command.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /setopencaptcha <question> | <answer1>, <answer2>, ...")
        return

    full_text = ' '.join(context.args)
    parts = full_text.split('|')
    if len(parts) != 2:
        await update.message.reply_text("Invalid format. Please use: question | answer1, answer2, ...")
        return

    question, answers_part = [part.strip() for part in parts]
    answers = [answer.strip().lower() for answer in answers_part.split(',')]

    chat_id = update.effective_chat.id
    
    connection = get_db_connection()
    if connection is None:
        await update.message.reply_text("Sorry, there was a problem connecting to the database. Please try again later.")
        return

    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO captchas (chat_id, mode, question, answers) VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE mode = %s, question = %s, answers = %s",
            (chat_id, "open", question, ','.join(answers), "open", question, ','.join(answers))
        )
        connection.commit()
        await update.message.reply_text(f"Open-ended captcha set. Question: {question}\nPossible answers: {', '.join(answers)}")
    except Error as e:
        print(f"Error setting open captcha: {e}")
        await update.message.reply_text("Sorry, there was a problem setting the captcha. Please try again later.")
    finally:
        cursor.close()
        connection.close()

async def set_multiple_captcha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await update.effective_chat.get_member(update.effective_user.id)
    if user.status not in ['creator', 'administrator']:
        await update.message.reply_text("Sorry, only admins can use this command.")
        return

    if len(context.args) < 3:
        await update.message.reply_text("Usage: /setmultiplechoice <question> | <correct_answer> | <wrong_answer1>, <wrong_answer2>, ...")
        return

    full_text = ' '.join(context.args)
    parts = full_text.split('|')
    if len(parts) != 3:
        await update.message.reply_text("Invalid format. Please use: question | correct answer | wrong answers")
        return

    question, correct_answer, wrong_answers_part = [part.strip() for part in parts]
    wrong_answers = [answer.strip() for answer in wrong_answers_part.split(',')]
    all_answers = [correct_answer] + wrong_answers

    chat_id = update.effective_chat.id
    
    connection = get_db_connection()
    if connection is None:
        await update.message.reply_text("Sorry, there was a problem connecting to the database. Please try again later.")
        return

    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO captchas (chat_id, mode, question, answers) VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE mode = %s, question = %s, answers = %s",
            (chat_id, "multiple", question, ','.join(all_answers), "multiple", question, ','.join(all_answers))
        )
        connection.commit()
        await update.message.reply_text(f"Multiple-choice captcha set. Question: {question}\nCorrect answer: {correct_answer}\nAll options: {', '.join(all_answers)}")
    except Error as e:
        print(f"Error setting multiple choice captcha: {e}")
        await update.message.reply_text("Sorry, there was a problem setting the captcha. Please try again later.")
    finally:
        cursor.close()
        connection.close()

async def set_welcome_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await update.effective_chat.get_member(update.effective_user.id)
    if user.status not in ['creator', 'administrator']:
        await update.message.reply_text("Sorry, only admins can use this command.")
        return

    if len(context.args) != 1:
        await update.message.reply_text("Usage: /setwelcometimeout <seconds>")
        return

    try:
        timeout = int(context.args[0])
        if timeout < 0:
            raise ValueError("Timeout must be non-negative")
    except ValueError:
        await update.message.reply_text("Please provide a valid non-negative number of seconds.")
        return

    chat_id = update.effective_chat.id
    
    connection = get_db_connection()
    if connection is None:
        await update.message.reply_text("Sorry, there was a problem connecting to the database. Please try again later.")
        return

    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO chat_settings (chat_id, welcome_timeout) VALUES (%s, %s) ON DUPLICATE KEY UPDATE welcome_timeout = %s",
            (chat_id, timeout, timeout)
        )
        connection.commit()
        await update.message.reply_text(f"Welcome message timeout set to {timeout} seconds.")
    except Error as e:
        print(f"Error setting welcome timeout: {e}")
        await update.message.reply_text("Sorry, there was a problem setting the welcome timeout. Please try again later.")
    finally:
        cursor.close()
        connection.close()

async def get_welcome_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    
    connection = get_db_connection()
    if connection is None:
        await update.message.reply_text("Sorry, there was a problem connecting to the database. Please try again later.")
        return

    cursor = connection.cursor()
    try:
        cursor.execute("SELECT welcome_timeout FROM chat_settings WHERE chat_id = %s", (chat_id,))
        result = cursor.fetchone()
        
        if result:
            timeout = result[0]
        else:
            timeout = 10  # Default welcome timeout if not set
        
        await update.message.reply_text(f"The current welcome message timeout is set to {timeout} seconds.")
    except Error as e:
        print(f"Error getting welcome timeout: {e}")
        await update.message.reply_text("Sorry, there was a problem retrieving the welcome timeout. Please try again later.")
    finally:
        cursor.close()
        connection.close()

async def delete_welcome_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    chat_id, message_id = job.data['chat_id'], job.data['message_id']
    
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        print(f"Welcome message (ID: {message_id}) deleted in chat {chat_id}")
    except TelegramError as e:
        print(f"Error deleting welcome message (ID: {message_id}) in chat {chat_id}: {e}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data.split(':')
    if data[0] == 'captcha':
        user_id = int(data[1])
        answer = data[2]

        connection = get_db_connection()
        if connection is None:
            await query.edit_message_text("Sorry, there was a problem processing your response. Please try again later.")
            return

        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute("SELECT * FROM pending_captchas WHERE user_id = %s", (user_id,))
            pending_captcha = cursor.fetchone()

            if not pending_captcha:
                await query.edit_message_text("This captcha is no longer valid.")
                return

            chat_id = pending_captcha['chat_id']
            captcha_message_id = pending_captcha['captcha_message_id']
            correct_answers = pending_captcha['correct_answers'].split(',')

            cursor.execute("SELECT * FROM chat_settings WHERE chat_id = %s", (chat_id,))
            chat_settings = cursor.fetchone()
            attempt_limit = chat_settings['attempt_limit'] if chat_settings else 3
            strict_mode = chat_settings['strict_mode'] if chat_settings else False
            welcome_message = chat_settings['welcome_message'] if chat_settings else f"Welcome to the group, {query.from_user.full_name}!"
            welcome_timeout = chat_settings['welcome_timeout'] if chat_settings else 10

            if answer.lower() in [ans.lower() for ans in correct_answers]:
                welcome_msg = await query.edit_message_text(f"Correct! {welcome_message}")
                cursor.execute("DELETE FROM pending_captchas WHERE user_id = %s", (user_id,))
                connection.commit()

                # Remove the kick job if it exists
                current_jobs = context.job_queue.get_jobs_by_name(f'kick_user_{chat_id}_{user_id}')
                for job in current_jobs:
                    job.schedule_removal()

                # Schedule welcome message deletion
                context.job_queue.run_once(
                    delete_welcome_message, 
                    welcome_timeout,
                    data={'chat_id': chat_id, 'message_id': welcome_msg.message_id},
                    name=f'delete_welcome_{chat_id}_{user_id}'
                )
            else:
                new_attempts = pending_captcha['attempts'] + 1
                cursor.execute("UPDATE pending_captchas SET attempts = %s WHERE user_id = %s", (new_attempts, user_id))
                connection.commit()

                if new_attempts >= attempt_limit:
                    # Schedule the kick job immediately
                    context.job_queue.run_once(
                        kick_user,
                        0,  # Run immediately
                        data={
                            'chat_id': chat_id,
                            'user_id': user_id,
                            'user_name': query.from_user.full_name,
                            'captcha_message_id': captcha_message_id,
                            'strict_mode': strict_mode
                        },
                        name=f'kick_user_{chat_id}_{user_id}'
                    )
                else:
                    remaining_attempts = attempt_limit - new_attempts
                    await query.edit_message_text(
                        f"Sorry, that's incorrect. You have {remaining_attempts} attempt{'s' if remaining_attempts > 1 else ''} remaining.\n\n"
                        f"Please try again: {pending_captcha['question']}",
                        reply_markup=query.message.reply_markup
                    )
        except Error as e:
            print(f"Database error in button_callback: {e}")
        finally:
            cursor.close()
            connection.close()

async def check_captcha_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    
    connection = get_db_connection()
    if connection is None:
        await update.message.reply_text("Sorry, there was a problem processing your response. Please try again later.")
        return

    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM pending_captchas WHERE user_id = %s", (user_id,))
        pending_captcha = cursor.fetchone()

        if not pending_captcha:
            return  # No pending captcha for this user

        chat_id = pending_captcha['chat_id']
        captcha_message_id = pending_captcha['captcha_message_id']
        correct_answers = pending_captcha['correct_answers'].split(',')
        messages_to_delete = json.loads(pending_captcha.get('messages_to_delete', '[]'))
        messages_to_delete.append(update.message.message_id)

        cursor.execute("SELECT * FROM chat_settings WHERE chat_id = %s", (chat_id,))
        chat_settings = cursor.fetchone()
        attempt_limit = chat_settings['attempt_limit'] if chat_settings else 3
        strict_mode = chat_settings['strict_mode'] if chat_settings else False
        welcome_message = chat_settings['welcome_message'] if chat_settings else f"Welcome to the group, {update.message.from_user.full_name}!"
        welcome_timeout = chat_settings['welcome_timeout'] if chat_settings else 10

        user_answer = update.message.text.strip().lower()

        if user_answer in [ans.lower() for ans in correct_answers]:
            success_message = await update.message.reply_text(f"Correct! {welcome_message}")
            messages_to_delete.append(success_message.message_id)
            cursor.execute("DELETE FROM pending_captchas WHERE user_id = %s", (user_id,))
            connection.commit()

            # Remove the kick job if it exists
            current_jobs = context.job_queue.get_jobs_by_name(f'kick_user_{chat_id}_{user_id}')
            for job in current_jobs:
                job.schedule_removal()

            # Schedule welcome message deletion
            context.job_queue.run_once(
                delete_welcome_message, 
                welcome_timeout,
                data={'chat_id': chat_id, 'message_id': success_message.message_id},
                name=f'delete_welcome_{chat_id}_{user_id}'
            )

            # Schedule captcha-related message deletion
            context.job_queue.run_once(
                delete_captcha_messages, 
                15, 
                data={'chat_id': chat_id, 'user_id': user_id, 'message_ids': messages_to_delete},
                name=f'delete_captcha_{chat_id}_{user_id}'
            )
        else:
            new_attempts = pending_captcha['attempts'] + 1
            
            if new_attempts >= attempt_limit:
                # Schedule the kick job immediately
                context.job_queue.run_once(
                    kick_user,
                    0,  # Run immediately
                    data={
                        'chat_id': chat_id,
                        'user_id': user_id,
                        'user_name': update.message.from_user.full_name,
                        'captcha_message_id': captcha_message_id,
                        'strict_mode': strict_mode,
                        'messages_to_delete': messages_to_delete
                    },
                    name=f'kick_user_{chat_id}_{user_id}'
                )
            else:
                remaining_attempts = attempt_limit - new_attempts
                reply_message = await update.message.reply_text(f"Sorry, that's incorrect. You have {remaining_attempts} attempts remaining.")
                messages_to_delete.append(reply_message.message_id)
                
                # Update the pending captcha with new attempt count and messages to delete
                cursor.execute("UPDATE pending_captchas SET attempts = %s, messages_to_delete = %s WHERE user_id = %s", 
                               (new_attempts, json.dumps(messages_to_delete), user_id))
                connection.commit()

    except Error as e:
        print(f"Database error in check_captcha_answer: {e}")
    finally:
        cursor.close()
        connection.close()

async def kick_user(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    chat_id = job.data['chat_id']
    user_id = job.data['user_id']
    user_name = job.data['user_name']
    captcha_message_id = job.data['captcha_message_id']
    strict_mode = job.data.get('strict_mode', False)

    connection = get_db_connection()
    if connection is None:
        print(f"Failed to connect to the database while trying to kick user {user_id} from chat {chat_id}")
        return

    cursor = connection.cursor(dictionary=True)
    try:
        # Check if the captcha is still pending
        cursor.execute("SELECT * FROM pending_captchas WHERE user_id = %s AND chat_id = %s", (user_id, chat_id))
        pending_captcha = cursor.fetchone()

        if pending_captcha:
            messages_to_delete = json.loads(pending_captcha.get('messages_to_delete', '[]'))
            messages_to_delete.append(captcha_message_id)

            try:
                # Send a message right before kicking the user
                temp_message = await context.bot.send_message(chat_id=chat_id, text=".")
                last_message_id = temp_message.message_id

                if strict_mode:
                    await context.bot.ban_chat_member(chat_id, user_id)
                    action_text = "banned permanently"
                else:
                    await context.bot.ban_chat_member(chat_id, user_id)
                    await context.bot.unban_chat_member(chat_id, user_id)
                    action_text = "removed"

                # Wait a moment for the system message to appear
                await asyncio.sleep(1)

                # Delete a range of messages to catch the system message
                for i in range(last_message_id, last_message_id + 5):
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=i)
                    except TelegramError:
                        pass  # Message doesn't exist or can't be deleted, move on

                # Delete all captcha-related messages
                for msg_id in messages_to_delete:
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                    except TelegramError as e:
                        print(f"Error deleting message {msg_id}: {e}")

                # Send a temporary notification about the action taken
                action_message = await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"{user_name} has been {action_text} for not completing the captcha."
                )
                await asyncio.sleep(5)  # Show the message for 5 seconds
                await context.bot.delete_message(chat_id=chat_id, message_id=action_message.message_id)

                # Remove the pending captcha from the database
                cursor.execute("DELETE FROM pending_captchas WHERE user_id = %s AND chat_id = %s", (user_id, chat_id))
                connection.commit()

            except TelegramError as e:
                print(f"Error kicking/banning user {user_id} from chat {chat_id}: {e}")

        else:
            print(f"Kick job ran for user {user_id} in chat {chat_id}, but they were not in pending_captchas.")

    except Error as e:
        print(f"Database error in kick_user: {e}")
    finally:
        cursor.close()
        connection.close()

def is_service_message(message: Message) -> bool:
    """
    Check if a message is a service message.
    """
    return (message.new_chat_members or 
            message.left_chat_member or 
            message.new_chat_title or 
            message.new_chat_photo or 
            message.delete_chat_photo or 
            message.group_chat_created or 
            message.supergroup_chat_created or 
            message.channel_chat_created or 
            message.message_auto_delete_timer_changed or
            message.pinned_message)

async def delete_welcome_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    chat_id, message_id = job.data['chat_id'], job.data['message_id']
    
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        print(f"Welcome message (ID: {message_id}) deleted in chat {chat_id}")
    except TelegramError as e:
        print(f"Error deleting welcome message (ID: {message_id}) in chat {chat_id}: {e}")

async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    join_message_id = update.message.message_id
    
    connection = get_db_connection()
    if connection is None:
        print("Failed to connect to the database")
        return

    cursor = connection.cursor(dictionary=True)
    
    for new_member in update.message.new_chat_members:
        user_id = new_member.id
        user_name = new_member.full_name

        try:
            # Get chat settings
            cursor.execute("SELECT * FROM chat_settings WHERE chat_id = %s", (chat_id,))
            settings = cursor.fetchone()
            timeout = settings['timeout'] if settings and 'timeout' in settings else 60
            attempt_limit = settings['attempt_limit'] if settings and 'attempt_limit' in settings else 3
            strict_mode = settings['strict_mode'] if settings else False

            # Get custom captcha if exists
            cursor.execute("SELECT * FROM captchas WHERE chat_id = %s", (chat_id,))
            custom_captcha = cursor.fetchone()

            if custom_captcha:
                mode, question, answers = custom_captcha['mode'], custom_captcha['question'], custom_captcha['answers']
                if mode == "open":
                    captcha_text = f"Welcome {user_name}!\n\nPlease answer this captcha within {timeout} seconds: {question}"
                    correct_answers = answers.split(',')
                    reply_markup = None
                elif mode == "multiple":
                    all_answers = answers.split(',')
                    correct_answer = all_answers[0]  # Assuming the first answer is correct
                    random.shuffle(all_answers)
                    captcha_text = f"Welcome {user_name}!\n\nPlease answer this captcha within {timeout} seconds:\n{question}"
                    keyboard = [[InlineKeyboardButton(answer, callback_data=f"captcha:{user_id}:{answer}")] for answer in all_answers]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    correct_answers = [correct_answer]
            else:
                question = "What is 2+2?"
                correct_answers = ["4", "four"]
                captcha_text = f"Welcome {user_name}!\n\nPlease answer this captcha within {timeout} seconds: {question}"
                reply_markup = None

            captcha_message = await context.bot.send_message(chat_id=chat_id, text=captcha_text, reply_markup=reply_markup)

            messages_to_delete = [captcha_message.message_id, join_message_id]
            cursor.execute("""
                INSERT INTO pending_captchas (user_id, chat_id, correct_answers, captcha_message_id, messages_to_delete, question)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (user_id, chat_id, ','.join(correct_answers), captcha_message.message_id, json.dumps(messages_to_delete), question))
            connection.commit()

            # Schedule job to kick user if they don't answer in time
            if context.job_queue:
                context.job_queue.run_once(
                    kick_user, 
                    timeout, 
                    data={
                        'chat_id': chat_id, 
                        'user_id': user_id, 
                        'user_name': user_name,
                        'captcha_message_id': captcha_message.message_id,
                        'strict_mode': strict_mode
                    },
                    name=f'kick_user_{chat_id}_{user_id}'
                )
            else:
                print(f"Warning: Job queue is not available. Unable to schedule kick job for user {user_id} in chat {chat_id}")

            print(f"New member {user_name} (ID: {user_id}) joined chat {chat_id}. Captcha sent.")

        except Error as e:
            print(f"Database error in handle_new_member: {e}")
        except TelegramError as e:
            print(f"Telegram error in handle_new_member: {e}")

    cursor.close()
    connection.close()

    # Try to delete the join message
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=join_message_id)
    except TelegramError as e:
        print(f"Error deleting join message: {e}")
        
async def captcha_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle captcha timeout."""
    job = context.job
    chat_id, user_id, user_name, timeout, message_id = job.data
    if user_id in pending_captchas:
        del pending_captchas[user_id]
        try:
            # Check if the user is still in the chat
            chat_member = await context.bot.get_chat_member(chat_id, user_id)
            if chat_member.status not in ['left', 'kicked']:
                # Attempt to kick the user
                await context.bot.ban_chat_member(chat_id, user_id)
                await context.bot.unban_chat_member(chat_id, user_id)  # Immediately unban to allow rejoining
                await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, 
                                                    text=f"{user_name} has been removed for not completing the captcha within {timeout} seconds.")
                print(f"User {user_id} kicked from chat {chat_id} due to captcha timeout after {timeout} seconds.")
            else:
                print(f"User {user_id} already left chat {chat_id} before captcha timeout of {timeout} seconds.")
        except TelegramError as e:
            print(f"Error kicking user {user_id} from chat {chat_id} after {timeout} seconds: {e}")
            if "Not enough rights" in str(e):
                await context.bot.send_message(chat_id, "I don't have permission to remove users. Please give me the necessary rights.")
        except Exception as e:
            print(f"Unexpected error kicking user {user_id} from chat {chat_id} after {timeout} seconds: {e}")
    else:
        print(f"User {user_id} not found in pending_captchas for chat {chat_id} after {timeout} seconds. They might have already answered correctly.")

async def delete_captcha_messages(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    chat_id = job.data['chat_id']
    user_id = job.data['user_id']
    messages_to_delete = job.data['messages_to_delete']

    for msg_id in messages_to_delete:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except TelegramError as e:
            print(f"Error deleting message {msg_id} in chat {chat_id}: {e}")

    print(f"Deleted all captcha-related messages for user {user_id} in chat {chat_id}")

async def check_permissions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check the bot's permissions in the chat."""
    chat_id = update.effective_chat.id
    bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
    
    if bot_member.status != 'administrator':
        await update.message.reply_text("I'm not an administrator in this chat. Please make me an admin to use all features.")
        return

    permissions = []
    if bot_member.can_delete_messages:
        permissions.append("Delete messages")
    if bot_member.can_restrict_members:
        permissions.append("Ban users")

    if permissions:
        await update.message.reply_text(f"I have the following relevant permissions: {', '.join(permissions)}")
    else:
        await update.message.reply_text("I don't have the necessary permissions. Please give me 'Delete messages' and 'Ban users' rights.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = """
🤖 CustomCaptchaBot Help 🤖

This bot helps protect your group from spam and unwanted users by requiring new members to solve a captcha.

Admin Commands:
/settimeout <seconds> - Set the time limit for solving the captcha (default: 60 seconds)
/gettimeout - Check the current timeout setting

/setattemptlimit <number> - Set the maximum number of attempts allowed (default: 3)
/getattemptlimit - Check the current attempt limit

/setwelcomemessage <message> - Set a custom welcome message
/getwelcomemessage - Check the current welcome message

/setwelcometimeout <seconds> - Set how long the welcome message is displayed (default: 10 seconds)
/getwelcometimeout - Check the current welcome message timeout

/setopencaptcha <question> | <answer1>, <answer2>, ... - Set an open-ended captcha
/setmultiplechoice <question> | <correct_answer> | <wrong_answer1>, <wrong_answer2>, ... - Set a multiple-choice captcha

/setstrictmode - Enable strict mode (permanently ban users who fail the captcha)
/unsetstrictmode - Disable strict mode (only kick users who fail the captcha)

/getallsettings - View all current settings for the chat

/checkpermissions - Check if the bot has the necessary permissions in the group

How to use:
1. Use /checkpermissions to ensure the bot has all required permissions.
2. Set up your desired captcha using either /setopencaptcha or /setmultiplechoice.
3. Adjust the timeout, attempt limit, welcome message, and welcome message timeout as needed.
4. Enable or disable strict mode based on your preferences.
5. The bot will automatically challenge new members when they join.

Captcha Types:
- Open-ended: Users must type the correct answer.
- Multiple-choice: Users select from given options.

Strict Mode:
- When enabled, users who fail the captcha are permanently banned.
- When disabled, users who fail are kicked but can rejoin.

Welcome Message:
- The bot displays a welcome message when a user correctly answers the captcha.
- This message is automatically deleted after the set welcome message timeout.

Tips:
- Use /getallsettings regularly to review your current configuration.
- Test your settings by having a non-admin account join the group.
- Adjust the timeouts based on the difficulty of your captcha and your group's needs.

For any issues or further assistance, please contact the bot developer @KulikovNikolay.
"""
    await update.message.reply_text(help_text)

async def handle_edited_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle edited command messages."""
    command = update.edited_message.text.split()[0].lower()
    if command == '/settimeout':
        await set_timeout(update, context)
    elif command == '/gettimeout':
        await get_timeout(update, context)
    elif command == '/setattemptlimit':
        await set_attempt_limit(update, context)
    elif command == '/getattemptlimit':
        await get_attempt_limit(update, context)
    # Add other commands here if needed

async def cleanup_pending_captchas(context: ContextTypes.DEFAULT_TYPE) -> None:
    connection = get_db_connection()
    if connection is None:
        print("Failed to connect to the database during cleanup")
        return

    cursor = connection.cursor()
    try:
        # Delete entries older than 2 hours
        two_hours_ago = datetime.now() - timedelta(hours=2)
        
        # First, select the entries to be deleted
        cursor.execute("SELECT user_id, chat_id FROM pending_captchas WHERE created_at < %s", (two_hours_ago,))
        old_entries = cursor.fetchall()
        
        for user_id, chat_id in old_entries:
            # Check if there's an active kick job for this user
            job_name = f'kick_user_{chat_id}_{user_id}'
            jobs = context.job_queue.get_jobs_by_name(job_name)
            
            if not jobs:  # If no active kick job, it's safe to delete
                cursor.execute("DELETE FROM pending_captchas WHERE user_id = %s", (user_id,))
                print(f"Cleaned up pending captcha for user {user_id} in chat {chat_id}")
        
        connection.commit()
    except Error as e:
        print(f"Error during cleanup of pending captchas: {e}")
    finally:
        cursor.close()
        connection.close()

import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def main() -> None:
    """Start the bot."""
    try:
        # Create the Application and pass it your bot's token.
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

        # Set up the job queue
        job_queue = application.job_queue

        # Command handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("settimeout", set_timeout))
        application.add_handler(CommandHandler("gettimeout", get_timeout))
        application.add_handler(CommandHandler("setattemptlimit", set_attempt_limit))
        application.add_handler(CommandHandler("getattemptlimit", get_attempt_limit))
        application.add_handler(CommandHandler("setopencaptcha", set_open_captcha))
        application.add_handler(CommandHandler("setmultiplechoice", set_multiple_captcha))
        application.add_handler(CommandHandler("setwelcomemessage", set_welcome_message))
        application.add_handler(CommandHandler("getwelcomemessage", get_welcome_message))
        application.add_handler(CommandHandler("setstrictmode", set_strict_mode))
        application.add_handler(CommandHandler("unsetstrictmode", unset_strict_mode))
        application.add_handler(CommandHandler("getallsettings", get_all_settings))
        application.add_handler(CommandHandler("checkpermissions", check_permissions))
        application.add_handler(CommandHandler("setwelcometimeout", set_welcome_timeout))
        application.add_handler(CommandHandler("getwelcometimeout", get_welcome_timeout))

        # Handle new chat members
        application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member))

        # Handle captcha button callbacks
        application.add_handler(CallbackQueryHandler(button_callback, pattern="^captcha:"))

        # Handle text messages (for open-ended captchas)
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, check_captcha_answer))

        # Handle edited messages for commands
        application.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.COMMAND, handle_edited_command))

        # Schedule the cleanup job to run every hour
        if job_queue:
            job_queue.run_repeating(cleanup_pending_captchas, interval=3600, first=10)
            # Schedule the group statistics update job to run once per day
            job_queue.run_daily(update_group_statistics, time=dt_time(0, 0, tzinfo=pytz.UTC))
        else:
            print("Warning: Job queue is not available. Scheduled tasks will not run.")

        # Start the Bot
        print("Starting the bot...")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

    except Exception as e:
        print(f"Error starting the bot: {e}")
        raise  # Re-raise the exception to ensure the service fails and logs the error

if __name__ == '__main__':
    main()