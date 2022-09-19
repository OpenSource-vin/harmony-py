vowels = ['a', 'e', 'i', 'o', 'u']
animal = 'elephant'

count = 0
for letter in animal:
    if letter in vowels:
        count += 1

print(count)
