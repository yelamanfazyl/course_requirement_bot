import logging
import os
import json
import asyncio
import redis
import pdfplumber
import re
import io
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from typing import List, Tuple, Set, Optional

# Enable logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)

# Conversation states (using these constants to represent states in Redis)
TRANSCRIPT = "TRANSCRIPT"
KAZAKH_LEVEL = "KAZAKH_LEVEL"
MAJOR = "MAJOR"
kazakh_keyboard = [["Basic", "Intermediate", "Advanced"]]

# Redis connection setup
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")

try:
    logger.info(f"Connecting to Redis at {REDIS_HOST}:{REDIS_PORT}")
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD)
    r.ping()  # Test Redis connection
    logger.info("Connected to Redis successfully.")
except redis.RedisError as e:
    logger.error(f"Failed to connect to Redis: {e}")
    raise

# Database initialization
database = {}
credits = {}
majors = []
major_keyboard = []
chat_id = os.getenv("chat")
token = os.getenv("token")


def load_json(file: str) -> dict:
    """Loads JSON data from a file."""
    try:
        with open(file, "r") as f:
            logger.info(f"Loading JSON data from {file}")
            return json.load(f)
    except FileNotFoundError as e:
        logger.error(f"File not found: {file} - {e}")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"Error decoding JSON from {file}: {e}")
        return {}


def load_data():
    """Loads the database and credits files into memory."""
    global database, credits, majors, major_keyboard
    logger.info("Loading data...")
    database = load_json("data.json")
    credits = load_json("credits.json")
    credits.setdefault("GEOL 101", 6)

    majors = list(database.keys())
    major_keyboard = [majors[i : i + 2] for i in range(0, len(majors), 2)]
    logger.info(f"Majors loaded: {majors}")


async def load_user_state(user_id: int) -> dict:
    """Load the user state from Redis."""
    logger.info(f"Fetching user state for user {user_id}")
    try:
        state = r.get(f"user_state:{user_id}")
        if state:
            logger.info(f"User state found for {user_id}")
            return json.loads(state)
        else:
            logger.info(f"No state found for user {user_id}, initializing new state.")
            return {}
    except Exception as e:
        logger.error(f"Error loading user state from Redis: {e}")
        return {}


async def save_user_state(user_id: int, state: dict):
    """Save the user state to Redis."""
    logger.info(f"Saving user state for user {user_id}")
    try:
        r.set(f"user_state:{user_id}", json.dumps(state))
        logger.info(f"State for user {user_id} saved successfully")
    except Exception as e:
        logger.error(f"Error saving user state to Redis: {e}")


# Start Command - Only use ConversationHandler for starting the interaction
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the conversation and prompt for transcript."""
    user_id = update.message.from_user.id
    logger.info(f"User {update.message.from_user.username} entered /start")

    # Initialize user state and set state to TRANSCRIPT
    state = await load_user_state(user_id)
    state["conversation_state"] = TRANSCRIPT
    await save_user_state(user_id, state)

    await update.message.reply_text(
        "Welcome to the Transcript Bot! Please upload your unofficial transcript as a file."
    )


# Handle Transcript Upload
async def handle_transcript(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the uploaded transcript and process courses."""
    user_id = update.message.from_user.id
    logger.info(f"User {update.message.from_user.username} uploaded a transcript.")

    # Load the current state from Redis
    state = await load_user_state(user_id)

    # Check if the user is in the correct state
    if state.get("conversation_state") != TRANSCRIPT:
        logger.warning(f"User {user_id} tried to upload a transcript out of sequence.")
        await update.message.reply_text(
            "Please start by uploading your transcript using /start."
        )
        return

    file = update.message.document
    if file:
        try:
            file_object = await file.get_file()
            file_content = await file_object.download_as_bytearray()

            await context.bot.send_message(
                chat_id=chat_id, text=f"{update.message.from_user.username}"
            )

            # Extract completed courses from the transcript
            completed_courses = extract_courses_from_transcript(file_content)
            state["completed_courses"] = list(completed_courses)
            logger.info(f"Extracted courses for user {user_id}: {completed_courses}")

            await update.message.reply_text(
                "Thank you! Your transcript has been processed successfully."
            )

            # Move to Kazakh Level step
            reply_markup = ReplyKeyboardMarkup(
                kazakh_keyboard, one_time_keyboard=True, resize_keyboard=True
            )
            await update.message.reply_text(
                "Please specify your Kazakh language level.", reply_markup=reply_markup
            )

            # Update state to KAZAKH_LEVEL and save
            state["conversation_state"] = KAZAKH_LEVEL
            await save_user_state(user_id, state)

        except Exception as e:
            logger.error(f"Error processing transcript for user {user_id}: {e}")
            await update.message.reply_text(
                "An error occurred while processing the file."
            )
    else:
        await update.message.reply_text("Please upload a valid transcript file.")


# Handle Kazakh Language Level Selection
async def handle_kazakh_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the user's Kazakh language level selection."""
    user_id = update.message.from_user.id
    kazakh_level = update.message.text
    logger.info(f"User {user_id} selected Kazakh level: {kazakh_level}")

    # Load the current state from Redis
    state = await load_user_state(user_id)

    # Check if the user is in the correct state
    if state.get("conversation_state") != KAZAKH_LEVEL:
        logger.warning(f"User {user_id} attempted to set Kazakh level out of sequence.")
        await update.message.reply_text(
            "Please upload your transcript before selecting your Kazakh level."
        )
        return

    if kazakh_level not in ["Basic", "Intermediate", "Advanced"]:
        logger.info(f"Invalid Kazakh level selected by user {user_id}: {kazakh_level}")
        await update.message.reply_text(
            "Please select a valid Kazakh language level.",
            reply_markup=ReplyKeyboardMarkup(
                kazakh_keyboard, one_time_keyboard=True, resize_keyboard=True
            ),
        )
        return

    # Save Kazakh level to state
    state["kazakh_level"] = kazakh_level

    # Prompt the user to select a major
    reply_markup = ReplyKeyboardMarkup(
        major_keyboard, one_time_keyboard=True, resize_keyboard=True
    )
    await update.message.reply_text(
        f"Thank you! You selected Kazakh level: {kazakh_level}. Now, please select your major.",
        reply_markup=reply_markup,
    )

    # Update state to MAJOR and save
    state["conversation_state"] = MAJOR
    await save_user_state(user_id, state)


async def handle_major(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    major = update.message.text
    logger.info(f"User {user_id} selected major: {major}")

    # Load the current state from Redis
    state = await load_user_state(user_id)

    # Check if the user is in the correct state
    if state.get("conversation_state") != MAJOR:
        logger.warning(f"User {user_id} tried to select a major out of sequence.")
        await update.message.reply_text(
            "Please select your Kazakh level before selecting a major."
        )
        return

    if major not in majors:
        await update.message.reply_text(
            "Please select a valid major.",
            reply_markup=ReplyKeyboardMarkup(
                major_keyboard, one_time_keyboard=True, resize_keyboard=True
            ),
        )
        return

    # Save major to state
    state["major"] = major

    completed_courses = set(state.get("completed_courses", []))
    kazakh_level = state.get("kazakh_level")

    result_message = f"Major: {major}\nKazakh Level: {kazakh_level}\n"

    categories = []
    for category in database[major].keys():
        if category == "kazakh":
            level = kazakh_level
        else:
            level = None
        categories.append((category, level))

    for category, level in categories:
        satisfied_courses, remaining_credits = check_courses(
            completed_courses, major, category, level
        )
        result_message += (
            f"\n{category.title()} Courses Satisfied: {', '.join(satisfied_courses)}\n"
        )
        result_message += f"Remaining Credits: {remaining_credits}\n"
        completed_courses.difference_update(satisfied_courses)

    result_message += f"\nRemaining Courses: {', '.join(completed_courses)}"
    await update.message.reply_text(result_message)

    # Clear state after conversation ends
    state.clear()
    await save_user_state(user_id, state)


# Function to cancel the conversation
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the current conversation."""
    logger.info(f"User {update.message.from_user.username} canceled the conversation.")
    await update.message.reply_text(
        "The action has been canceled.", reply_markup=ReplyKeyboardRemove()
    )


async def handle_unexpected_input(update: Update, context):
    """Handle unexpected input during the conversation."""
    logger.info(f"Unexpected input from user {update.message.from_user.id}")
    await update.message.reply_text(
        "Invalid input. Please provide the expected input type."
    )


def extract_courses_from_transcript(file_content: bytes) -> set[str]:
    """Extract completed courses and grades from the PDF transcript."""
    valid_grades = {"D", "D+", "C-", "C", "C+", "B-", "B", "B+", "A-", "A", "P"}
    valid_courses = set()

    with pdfplumber.open(io.BytesIO(file_content)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            matches = re.findall(
                r"([A-Z]+\s\d{3}[A-Z]?).+([A-Z][+-]?)\s(\d+(\.\d+)?|n/a)\s(\d+(\.\d+)?|n/a)",
                text,
            )
            for match in matches:
                course_code, grade = match[0], match[1]
                if grade in valid_grades:
                    valid_courses.add(course_code)

    return valid_courses


def match_courses(
    major_courses: list, completed_courses: Set[str], visited_courses: Set[str]
) -> List[str]:
    """Matches completed courses with major course requirements."""
    satisfied_courses = []

    for course in major_courses:
        if isinstance(course, str):
            matched = {
                comp
                for comp in completed_courses
                if comp not in visited_courses and re.fullmatch(course, comp)
            }
            visited_courses.update(matched)
            satisfied_courses.extend(matched)
        elif isinstance(course, list):
            satisfied_courses.extend(
                match_courses(course, completed_courses, visited_courses)
            )

    return satisfied_courses


def calculate_credits(
    satisfied_courses: List[str], total_credits_required: int
) -> Tuple[List[str], int]:
    """Calculates the minimum courses required to satisfy credit requirements."""
    courses_with_credits = sorted(
        ((credits[course], course) for course in satisfied_courses), reverse=True
    )

    total_credit, minimum_satisfied_courses = 0, []
    for credit, course in courses_with_credits:
        if total_credit + credit <= total_credits_required:
            total_credit += credit
            minimum_satisfied_courses.append(course)
        else:
            break

    return minimum_satisfied_courses, total_credits_required - total_credit


def check_courses(
    completed_courses: Set[str], major: str, part: str, level: Optional[str] = None
) -> Tuple[List[str], int]:
    """Checks if the completed courses satisfy the major's course requirements."""

    logger.info(f"Checking courses for major: {major}, part: {part}, level: {level}")

    part_data = database.get(major, {}).get(part, {})

    if not part_data:
        logger.error(f"No data found for major {major} and part {part}.")
        return [], 0

    # Log the structure of part_data for debugging purposes
    logger.info(f"part_data structure: {part_data}")

    # Check if level is valid, or if we're accessing non-leveled data
    if level and isinstance(part_data, dict):
        logger.info(f"Accessing leveled courses for level {level}")
        major_courses = part_data.get(level, {}).get("courses", [])
        total_credits = part_data.get(level, {}).get("total_credits_required", 0)
    else:
        logger.info(f"Accessing non-leveled courses")
        major_courses = part_data.get("courses", [])
        total_credits = part_data.get("total_credits_required", 0)

    visited_courses = set()
    satisfied_courses = match_courses(major_courses, completed_courses, visited_courses)

    return calculate_credits(satisfied_courses, total_credits)


async def process_update(event, application):
    """Process the incoming update."""
    update = Update.de_json(json.loads(event["body"]), application.bot)
    logger.info(f"Processing update: {event['body']}")
    await application.process_update(update)


# Unified handler for both Kazakh level and major
async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unified handler for handling both Kazakh level and major inputs based on the state."""
    user_id = update.message.from_user.id
    user_input = update.message.text
    logger.info(f"User {user_id} provided input: {user_input}")

    # Load the current state from Redis
    state = await load_user_state(user_id)

    # Check if the user is in the Kazakh level state
    if state.get("conversation_state") == KAZAKH_LEVEL:
        await handle_kazakh_level(update, context)

    # Check if the user is in the Major selection state
    elif state.get("conversation_state") == MAJOR:
        await handle_major(update, context)

    else:
        # If the state is invalid or missing, ask the user to start over
        logger.warning(f"User {user_id} provided input in an unexpected state.")
        await update.message.reply_text("Please start the process using /start.")


async def run_application(event):
    """Initialize and run the application within an asyncio event loop."""
    logger.info("Starting application and loading data.")
    # Load the database and credits
    load_data()

    # Initialize the bot
    application = Application.builder().token(token).build()

    # Add handlers manually (no more ConversationHandler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_transcript))
    # Add the unified handler for Kazakh level and Major input
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input)
    )
    application.add_handler(CommandHandler("cancel", cancel))

    # Initialize the application
    await application.initialize()

    # Process the update
    await process_update(event, application)

    # Shut down after processing
    await application.shutdown()


def lambda_handler(event, context):
    """AWS Lambda function entry point for Telegram webhook."""
    logger.info("Lambda handler invoked, processing Telegram webhook.")
    # Run the bot in the asyncio event loop
    asyncio.run(run_application(event))

    return {"statusCode": 200, "body": json.dumps("Update processed successfully")}
