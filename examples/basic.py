"""Demonstrate indexed retrieval, historical lookup and rollback."""
from chronodict import ChronoDict

employees = ChronoDict(indexes=['department', 'salary'])
employees[101] = {'name': 'Alex', 'department': 'IT', 'salary': 50000}
employees.patch(101, salary=70000)
employees.patch(101, department='Finance')
print('Current:', employees[101])
print('Historical:', employees.find(department='IT', version=1))
print('History:', employees.history(101))
employees.rollback(101, version=1)
print('Restored:', employees[101])
