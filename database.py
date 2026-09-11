import sqlite3


class Storage:

    def __init__(self):
        # Fast in-memory SQLite database
        self.connection = sqlite3.connect(":memory:")

        # Create database schema
        self.create_table()

    def create_table(self):

        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS variable_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                line_number INTEGER NOT NULL,
                variable_name TEXT NOT NULL,
                serialized_value TEXT
            )
        """)

        # Index for timeline/history queries
        self.connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_timestamp
            ON variable_states(timestamp)
        """)

        # Index for finding a particular variable
        self.connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_variable
            ON variable_states(variable_name)
        """)

        # Index for finding variable state at a particular line
        self.connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_line_variable
            ON variable_states(line_number, variable_name)
        """)

        self.connection.commit()

    # Save variable state
    def save_state(
        self,
        timestamp,
        line_number,
        variable_name,
        serialized_value
    ):

        self.connection.execute("""
            INSERT INTO variable_states
            (
                timestamp,
                line_number,
                variable_name,
                serialized_value
            )
            VALUES (?, ?, ?, ?)
        """, (
            timestamp,
            line_number,
            variable_name,
            serialized_value
        ))

        self.connection.commit()

    # Get all stored states
    def get_states(self):

        cursor = self.connection.execute("""
            SELECT
                timestamp,
                line_number,
                variable_name,
                serialized_value
            FROM variable_states
            ORDER BY timestamp
        """)

        return cursor.fetchall()

    # Close database
    def close(self):
        self.connection.close()


# --------------------------------------------------
# TESTING
# --------------------------------------------------

if __name__ == "__main__":

    storage = Storage()

    # Example variable states
    storage.save_state(1, 1, "x", "10")
    storage.save_state(2, 2, "y", "20")
    storage.save_state(3, 3, "x", "30")
    storage.save_state(4, 4, "z", "60")

    # Display stored data
    states = storage.get_states()

    print("Stored Variable States:")
    print("-" * 50)

    for state in states:
        print(state)

    storage.close()