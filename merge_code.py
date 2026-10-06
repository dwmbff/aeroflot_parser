import os

def merge_python_files():
    output_filename = "all_code.txt"
    root_dir = os.getcwd()
    
    with open(output_filename, "w", encoding="utf-8") as outfile:
        # Обходим все папки в проекте
        for root, dirs, files in os.walk(root_dir):
            # Игнорируем папку виртуального окружения .venv и скрытые папки типа .git или .idea
            if any(ignored in root for ignored in [".venv", "venv", ".git", ".idea", "__pycache__"]):
                continue
                
            for file in files:
                # Берем только файлы с кодом Python, исключая сам этот скрипт-склейщик
                if file.endswith(".py") and file != "merge_code.py":
                    file_path = os.path.join(root, file)
                    relative_path = os.path.relpath(file_path, root_dir)
                    
                    # Пишем красивый заголовок для нейросети
                    outfile.write(f"\n\n=== FILE: {relative_path} ===\n")
                    
                    # Читаем файл в правильной кодировке UTF-8 и записываем его
                    try:
                        with open(file_path, "r", encoding="utf-8") as infile:
                            outfile.write(infile.read())
                    except Exception as e:
                        outfile.write(f"[Ошибка чтения файла: {e}]\n")
                        
                    outfile.write("\n====================\n")
                    
    print("Успех! Все файлы склеены без ошибок в кодировке в файл all_code.txt")

if __name__ == "__main__":
    merge_python_files()
