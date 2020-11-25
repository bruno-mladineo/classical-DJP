import sys

from ase.io import read, write

from ase.visualize import view

from ase import Atoms

mol = str("4127193.cif")

string = str.split(mol, '.')

Molecule = Atoms()
atoms = read(mol)
#'''
z_max = atoms.get_cell()[2][2]
print(z_max)

unit_cell = [0, 0, 0]

for atom in atoms:
    if atom.index % 4 == 0:
        if atom.position[2] >= z_max/2:
            atom.position = atom.position - [0, 0, z_max/2]         
        print(atom.position)
        Molecule.append(atom)
#        if unit_cell[0] <= atom.position[0]: unit_cell[0] = atom.position[0]
#        if unit_cell[1] <= atom.position[1]: unit_cell[1] = atom.position[1]
#        if unit_cell[2] <= atom.position[2]: unit_cell[2] = atom.position[2]
            
unit_cell = atoms.get_cell()
unit_cell[2] =unit_cell[2]/2
Molecule.set_cell(unit_cell)
print(Molecule.get_cell())
print(atoms.get_cell())

view(Molecule)
#'''
#write(string[0] + '.xyz', atoms)
#print(atoms.get_chemical_formula())