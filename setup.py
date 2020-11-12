from setuptools import setup, find_packages

setup(name='ocr4all_helpers',
      version='0.3.0',
      description='Different python scripts used in the OCR4all workflow.',
      url='https://github.com/OCR4all/OCR4all_helper-scripts',
      author='Nico Balbach, Maximilian Nöth',
      author_email='nico.balbach@informatik.uni-wuerzburg.de, maximilian.noeth@protonmail.com',
      packages=find_packages(),
      license='GPL-v3.0',
      entry_points={
            'console_scripts': [
                  'pagelineseg=ocr4all_helpers.pagelineseg:cli',
                  'skewestimate=ocr4all_helpers.skewestimate:cli',
                  'pagedir2pagexml=ocr4all_helpers.pagedir2pagexml:main',
                  'legacy_convert=ocr4all_helpers.legacyconvert:main'
            ],
      },
      install_requires=open("requirements.txt").read().split(),
      zip_safe=False)
