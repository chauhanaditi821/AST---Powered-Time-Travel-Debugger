import ast
import copy
import sqlite3
import json
import traceback


SOURCE_CODE = """
x = 10
y = 20
z = x + y

numbers = [1, 2, 3]
numbers.append(z)

result = z * 2

print("Result:", result)
"""


DATABASE_NAME = "execution_history.db"


def parse_source_code(source_code):
    try:
        return ast.parse(source_code)
    except SyntaxError as error:
        print("Syntax Error:")
        print(error)
        return None


def get_statements(tree):
    return tree.body


def display_ast(tree):
    print("\nAST")
    print("=" * 50)
    print(ast.dump(tree, indent=4))


def create_database():
    connection = sqlite3.connect(DATABASE_NAME)

    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS execution_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            step INTEGER,
            statement TEXT,
            variables TEXT
        )
    """)

    connection.commit()

    return connection


def save_snapshot(connection, step, statement, variables):
    cursor = connection.cursor()

    variables_json = json.dumps(
        variables,
        default=str
    )

    cursor.execute("""
        INSERT INTO execution_history
        (step, statement, variables)
        VALUES (?, ?, ?)
    """, (
        step,
        statement,
        variables_json
    ))

    connection.commit()


def get_history():
    connection = sqlite3.connect(DATABASE_NAME)

    cursor = connection.cursor()

    cursor.execute("""
        SELECT step, statement, variables
        FROM execution_history
        ORDER BY step
    """)

    history = cursor.fetchall()

    connection.close()

    return history


def get_snapshot(step):
    connection = sqlite3.connect(DATABASE_NAME)

    cursor = connection.cursor()

    cursor.execute("""
        SELECT step, statement, variables
        FROM execution_history
        WHERE step = ?
    """, (step,))

    snapshot = cursor.fetchone()

    connection.close()

    return snapshot


def capture_variables(environment):
    variables = {}

    for name, value in environment.items():

        if not name.startswith("__"):

            try:
                variables[name] = copy.deepcopy(value)

            except Exception:
                variables[name] = str(value)

    return variables


def execute_statement(statement, environment):

    try:

        module = ast.Module(
            body=[statement],
            type_ignores=[]
        )

        compiled_code = compile(
            module,
            filename="<ast_debugger>",
            mode="exec"
        )

        exec(compiled_code, environment)

        return True

    except Exception:

        print("\nError while executing statement:")
        traceback.print_exc()

        return False


def execute_program(source_code):

    tree = parse_source_code(source_code)

    if tree is None:
        return

    statements = get_statements(tree)

    connection = create_database()

    environment = {
        "__builtins__": __builtins__
    }

    print("\nAST Statements")
    print("=" * 50)

    for step, statement in enumerate(statements, start=1):

        statement_source = ast.unparse(statement)

        print(f"\nStep {step}")
        print("-" * 40)

        print("Statement:")
        print(statement_source)

        success = execute_statement(
            statement,
            environment
        )

        if not success:
            print("\nExecution stopped.")
            break

        variables = capture_variables(environment)

        print("\nVariables:")

        for name, value in variables.items():
            print(f"{name} = {value}")

        save_snapshot(
            connection,
            step,
            statement_source,
            variables
        )

    connection.close()


def show_execution_history():

    history = get_history()

    print("\nExecution History")
    print("=" * 60)

    if not history:
        print("No execution history found.")
        return

    for step, statement, variables in history:

        print(f"\nStep {step}")
        print("-" * 40)

        print("Statement:")
        print(statement)

        print("\nVariables:")

        variable_data = json.loads(variables)

        for name, value in variable_data.items():
            print(f"{name} = {value}")


def time_travel(step):

    snapshot = get_snapshot(step)

    if snapshot is None:
        print(f"\nNo snapshot found for step {step}")
        return

    step_number, statement, variables = snapshot

    variable_data = json.loads(variables)

    print("\nTime-Travel Debugging")
    print("=" * 60)

    print(f"Step: {step_number}")

    print("\nStatement:")
    print(statement)

    print("\nProgram State:")

    for name, value in variable_data.items():
        print(f"{name} = {value}")


def main():

    print("=" * 60)
    print("       PYTHON TIME-TRAVEL DEBUGGER")
    print("=" * 60)

    print("\nPython Source Code")
    print("=" * 60)
    print(SOURCE_CODE)

    print("\nParsing Source Code...")
    print("=" * 60)

    tree = parse_source_code(SOURCE_CODE)

    if tree is None:
        return

    display_ast(tree)

    print("\nExecuting Program...")
    print("=" * 60)

    execute_program(SOURCE_CODE)

    show_execution_history()

    print("\n")
    print("=" * 60)
    print("TIME-TRAVEL DEBUGGING")
    print("=" * 60)

    while True:

        choice = input(
            "\nEnter step number to inspect "
            "or 'q' to quit: "
        )

        if choice.lower() == "q":
            print("\nDebugger closed.")
            break

        try:

            step = int(choice)

            time_travel(step)

        except ValueError:

            print("Please enter a valid step number.")


if __name__ == "__main__":
    main()