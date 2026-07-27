import sqlite3
import os

# Define the path to your database file
# Assuming bot.db is in the same directory where you run this script
db_path = "./bot.db"

# Check if the database file exists
if not os.path.exists(db_path):
    print(f"Error: Database file not found at {db_path}")
else:
    try:
        # Connect to the SQLite database
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Query the paper_orders table
        cursor.execute("SELECT COUNT(*) FROM paper_orders")
        trade_count = cursor.fetchone()[0]

        print(f"Your bot has run {trade_count} trades in paper mode.")

        # Close the connection
        conn.close()
    except sqlite3.Error as e:
        print(f"Database error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
