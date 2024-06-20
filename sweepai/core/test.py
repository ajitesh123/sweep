import pdb

def factorial(n):
    # Insert a breakpoint
    pdb.set_trace()
    if n == 0:
        return 1
    else:
        return n * factorial(n-1)

# The script will pause here when you run it, allowing you to inspect the program's state
number = 5
result = factorial(number)
print(f"The factorial of {number} is {result}.")