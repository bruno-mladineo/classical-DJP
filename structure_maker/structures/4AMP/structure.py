#usr/bin/python

import numpy as np
import sys
import os

from ase.io import read, write
#from ase.io.trajectory import Trajectory, TrajectoryWriter
from ase import neighborlist
from ase import Atoms
from ase.build import sort

from scipy import sparse


############# The Dion-Jacobson version of the program #############


# Function get_molecule_length(mol):
####
#### Input:  ASE atoms object (molecule)
#### Output: The distance between the two N atom, in case of regular cell it is
####         the approximate distance between the inorganic layers

def get_molecule_length(mol):           
    N_indices = np.where(mol.symbols == 'N')    
    
    length = mol.get_distances(N_indices[0][0], N_indices[0][1])[0]
    
    return length


# Function normalized(a, axis=-1, order=2)<
####
#### Input:  List of vectors
#### Output: List of normalized (unit) vectors

def normalized(a, axis=-1, order=2):
    l2 = np.atleast_1d(np.linalg.norm(a, order, axis))
    l2[l2==0] = 1
    return a / np.expand_dims(l2, axis)


# Function get_rotation_matrix(a,b):
####
#### Input:  Two vectors a and b
#### Output: Rotation matrix that rotates a into b,
#### https://math.stackexchange.com/questions/180418/calculate-rotation-matrix-to-align-vector-a-to-vector-b-in-3d

def get_rotation_matrix(a,b):
    scalar = np.dot(a,b)
    cross = np.cross(a,b)
    I = np.identity(3)
    V = np.array(([0., -cross[2], cross[1]],[cross[2], 0., -cross[0]],[-cross[1], cross[0], 0.]))
    VV = np.dot(V,V)
    
    R = I + V + VV * 1. / (1. + scalar)
    
    return(R)


# First we create empty ASE Atoms objects in which we will write atoms
# 1) ordered ===   This will be our final structure
# 2) upper_MA ===  If we have a supercell, we will write MA molecules from the upper layer here
# 3) bottom_MA === MA from bottom layer. If no supercell, this will be all MA molecules

ordered = Atoms()
upper_MA = Atoms()
bottom_MA = Atoms()


# We ask the user which unit cell type they want. The x and y lengths of the 
# cell are measured in the units of Pb-Br distance
### cell_type = 1 === cell length is sqrt(2)
### cell_type = 2 === cell length is 2
### cell_type = 3 === cell lenght is 1

print('Enter unit cell type (1 or 2 or 3):')
print('X and Y cell lengths:')
print('1 -> sqrt(2) x sqrt(2)')
print('2 -> 2x2')
print('3 -> 1x1')

cell_type = str(input())

if (cell_type != '1' and cell_type != '2' and cell_type != '3'):
    sys.exit('Invald input, start again.')


# We ask the users how many inorganic layers they want.

print('Enter number of inorganic layers (n), available options are 1, 2, and 3:')

n = int(input())

if (n != 1 and n != 2 and n != 3):
    sys.exit('Invald input, start again.')


# We ask the user if they want a supercell.

print('Do you want a supercell in the z-direction (2 units)? (enter y/n)')

sup = str(input())
sup = sup.lower()

if (sup != 'y' and sup != 'n'):    
    sys.exit('Invald input, start again.')


if (sup == 'y'):
    super_cell  = 'super'

if (sup == 'n'):
    super_cell = 'unit'


# We ask the user if they want the upper layer to be directly above or offset
# for half xy cell length with regards to the bottom layer.
# This is only relevant if a supercell is picked.

if (super_cell == 'super'):
    print('Do you want the cell to be regular or offset? (enter r/o)')
       
    ro = str(input())
    ro = ro.lower()
        
    if (ro != 'r' and ro != 'o' and ro != ''):
        sys.exit('Invald input, start again.')
        
    if (ro == 'r' or ro == ''):
        reof = 'regular'
    if (ro == 'o'):
        reof = 'offset'
else: reof = 'regular'  # required for frame designation

# We ask the use if they want the organic molecules to alternate (every other organic
# molecule is mirror flipped in the z direction).
# Only relevant if cell type 1 and 2.

if (cell_type == '1' or cell_type == '2'):
    print('Do you want the organic molecules to alternite in their orientation? (y/n)')
    
    al = str(input())
    al = al.lower()
    
    if (al != 'y' and al != 'n' and al != ''):
        sys.exit('Invalid input, start again.')
    
    if (al == 'y'):
        alter = 'a'
    if (al == 'n' or al == ''):
        alter = 'na'

else: alter = 'na'  # is this needed?  probably not, but it can't hurt


### New:
# If a supercell is picked the distance between the top and bottom parts of the structure 
# will be delta_z_factor*molecule_length*offset_projection_factor (if cell is chosen to
# be regular offset_projection_factor = 1). If the unit cell is chosen, this will be the 
# seperation between the bottom and the upper long organic molecules. 

print('Enter inorganic layer separation in units of molecule length [default = 1.0]:') 

delta_z_factor = input()

if (len(delta_z_factor) == 0):
    delta_z_factor = float('1.0')
else:
    delta_z_factor = float(delta_z_factor)


# Here the unit cell can be additionally lengthened in the z-direction. 
# Otherwise the unit cell length will just be the difference between the 
# z coordinates of the lowest and highest atoms.

# why is this an absolute ammont and not relative cell_z_factor*molecule_length??
print('Enter the amount by witch you want to lengthen the unit cell in z-direction [default = 0.0]:')

cell_z_factor = input()

if (len(cell_z_factor) == 0):
    cell_z_factor = float('0.0')
else:
    cell_z_factor = float(cell_z_factor)


# Depending on the chosen options,
# template structure is read from the INORGANIC_FRAME_DIR directory

frame = read(os.environ["INORGANIC_FRAME_DIR"] + cell_type + 'n' + str(n) + '_' + reof + '_' + super_cell + '.traj')

# The long organic molecule is read from the script argument (e.g. 3AMP.traj, PEA.traj, BZA.traj, ...)

#mol = read('3AMP.traj')
mol = read(str(sys.argv[1]))

# We create lists of indices of different parts of the inorganic template structre

symbols = np.asarray(frame.get_chemical_symbols(), dtype='str')

Pb_indices = np.flatnonzero(symbols == 'Pb')
Br_indices = np.flatnonzero(symbols == 'Br')
N_indices = np.flatnonzero(symbols == 'N')
C_indices = np.flatnonzero(symbols == 'C')
H_indices = np.flatnonzero(symbols == 'H')

organic_indices = np.concatenate((N_indices, C_indices, H_indices)) 
inorganic_indices = np.concatenate((Pb_indices, Br_indices))


# We create seperate structures containing only organic and inorganic atoms

organic = frame[organic_indices]
inorganic = frame[inorganic_indices]


##### Looking at the organic part, currently we have only indices of C, N, O atoms,
##### but we don't know which atoms belong to MA molecules, and which belong to long
##### organic molecules. In the following part this is recognized and we obtain objects
##### "MA" and "Long" which contain these atoms. To this end we use ASE neighborlists:
##### see e.g. https://wiki.fysik.dtu.dk/ase/ase/neighborlist.html#ase.neighborlist.get_connectivity_matrix

cutOff = neighborlist.natural_cutoffs(organic)
neighborList = neighborlist.NeighborList(cutOff, self_interaction=False, bothways=True)
neighborList.update(organic)

matrix = neighborList.get_connectivity_matrix()

n_components, component_list = sparse.csgraph.connected_components(matrix)

MA=np.empty((0), dtype='int')
MA_counter = 0

Long=np.empty((0), dtype='int')
Long_counter = 0

# We separate the MA+ molecule from the rest knowing that they have 8 atoms (CH6N) 

for i in range(n_components):
    molIdx=i
    molIdxs = [ j for j in range(len(component_list)) if component_list[j] == molIdx ]
    if(len(molIdxs) == 8):
        MA = np.append(MA, molIdxs, axis=0)
        MA_counter += 1
    if(len(molIdxs) > 8):  
        Long = np.append(Long, molIdxs, axis=0)
        Long_counter += 1

# We have to use if(len(MA)) condition because if n=1 we don't have any MA

if(len(MA)):
    MA = np.split(MA, MA_counter) # np.split(x, y) splits x into y equal parts

Long = np.split(Long, Long_counter)


##### Now we find vectors from N atoms to molecule COM (Center Of Mass) 
##### for every long molecule in the template structure.
##### Also we store positions of N atoms.

N_com = np.empty((len(Long), 3)) # array of vectors
N_pos = np.empty((len(Long), 3)) # array of positions
counter = 0 #this counter just counts on which long molecule we are

for long in Long:
    long_symbols = np.asarray(organic[long].get_chemical_symbols(), dtype='str')
    N_index = np.asscalar(np.flatnonzero(long_symbols == 'N'))
    N_atom = organic[long[N_index]]
    N_position = N_atom.position
    N_pos[counter] = N_position
    N_com[counter] = organic[long].get_center_of_mass() - N_position
    counter += 1

N_com = normalized(N_com) #we need just the unit vectors


##### Now we find the positions of N atoms to use as an anchor 
##### Conditions for anker: The N atom is on the upper side of the 
##### inorganic layer -> This means the vector points up (z>0)

anchor_N_index = np.empty((0), dtype = 'int')
anchor_N_counter = 0

for i in range(0, len(N_pos)):
    if(N_com[i][2] > 0):
        anchor_N_index = np.append(anchor_N_index, [i], axis = 0)
        anchor_N_counter += 1

if(anchor_N_counter):
    anchor_N_index = np.split(anchor_N_index, anchor_N_counter)
else:
    sys.exit('No anchor sites found.')


###### Get the vector of the input molecule (3AMP.traj ...) that points from the first
###### N atom to the second N atom, whitch is first and whitch is second is irrelevant

mol_symbols = np.asarray(mol.get_chemical_symbols(), dtype='str')
N_mol_indices = np.flatnonzero(mol_symbols == 'N')
N_mol_pos_first = mol[N_mol_indices[0]].position
N_mol_pos_second = mol[N_mol_indices[1]].position 
N_mol_vector = N_mol_pos_second - N_mol_pos_first
N_mol_vector = normalized(N_mol_vector)[0]


###### Now we separate the inorganic frame from the template into top and bottom parts
###### so we can easily increase/decrease the distance between them to accomodate the
###### long molecules. In the case of unit cell (super_cell == 'unit'), this seperation doesn't
###### do anything because we have only one inorganic layer in the template.


## should this be made into if(super), if(unit)? in effect not that inportant as it is run only once, but still

inorganic_z = inorganic.get_positions()[:,2]

average_z = np.average(inorganic_z)

inorganic_upper = inorganic[np.where(inorganic_z > average_z)[0]]
inorganic_bottom = inorganic[np.where(inorganic_z <= average_z)[0]]

if (super_cell == 'super'):
    if(len(inorganic_upper) != len(inorganic_bottom)):
        print('Warning: different number of atoms in inorganic top and bottom parts, is this expected?')

inorganic_upper_z = inorganic_upper.get_positions()[:,2]
inorganic_bottom_z = inorganic_bottom.get_positions()[:,2]


# inorganic_z_distance is the distance between highest inorganic atom in bottom layer
# and lowest inorganic atom in upper layer.

inorganic_z_distance = np.amin(inorganic_upper_z) - np.amax(inorganic_bottom_z)

# If we have a unit cell, there will be no upper layer.

if (sup == 'n'):
    inorganic_z_distance = 0


# Approximate molecule length is obtained by the get_molecule_length(mol) function.

molecule_length = get_molecule_length(mol)


##### Here we find the vector pointing from the position of the molecule anchor
##### on the bottom inorganic layer to the anchor point on the upper inorganic layer.
##### If there is no offset the vector is in the z direction and the 
##### offset_projection_factor = 1.0. If there is an offset then the 
##### upper inorganic layer is translated. In case of 2x2 (type 2) cell by 1/4*unit cell
##### lenght in x and also in y direction. In cese of type 1 cell by 1/2 unit cell lenght
##### lenght in x direction?? The offset projection factor can be found using the
##### Pythagorean theorem. In case of type 3 cell (1x1) by 1/2 unit cell lenghts in
##### both x and y directions.

desired_direction = np.zeros(3)

if (reof == 'regular'):
    
    desired_direction[2] = 1.
    offset_projection_factor = 1.
    # if alternating, we want to know the dimensions of the molecule so that we can
    # translate properly when flipping ??
    x = 0
    y = 0
    z = molecule_length
    
if (reof == 'offset'):
    
    if(cell_type == '2'):
        
        x = mol.cell[0][0]/4
        y = mol.cell[1][1]/4
        z = np.sqrt(molecule_length**2 - x**2 - y**2)

        desired_direction[0] = x
        desired_direction[1] = y
        desired_direction[2] = z
        
        desired_direction = normalized(desired_direction)[0]

        offset_projection_factor = z / molecule_length


    if(cell_type == '1'):
        
        x = 0
        y = mol.cell[1][1]*(-0.35)  # 0.35 ~ sqrt(2)/4
        z = np.sqrt(molecule_length**2 - x**2 - y**2)
        
        desired_direction[0] = x
        desired_direction[1] = y
        desired_direction[2] = z
        
        desired_direction = normalized(desired_direction)[0]

        offset_projection_factor = z / molecule_length
        
    if(cell_type == '3'):
        
        x = mol.cell[0][0]/2
        y = mol.cell[1][1]/2
        z = np.sqrt(molecule_length**2 - x**2 - y**2)

        desired_direction[0] = x
        desired_direction[1] = y
        desired_direction[2] = z
        
        desired_direction = normalized(desired_direction)[0]

        offset_projection_factor = z / molecule_length
   
    
# delta_z tells us how much will we have to adjust the inorganic frame distance.

delta_z = delta_z_factor * molecule_length * offset_projection_factor - inorganic_z_distance

# In the case of supercell, the upper layer positions are adjusted by delta_z

if (sup == 'y'):
    inorganic_upper.positions[:,2] += delta_z

# Finally the bottom and the adjusted upper inorganic frames are brought back together.
# In the case of unit cell, we didn't do anything.

inorganic_all = inorganic_bottom + inorganic_upper

##### Since we moved the upper inorganic frame, now we check which MA's from the template
##### belong to the upper and bottom parts so we can adjust their positions accordingly.

if(n-1):  # Reminder, n is the number of inorganic layers, there is no MA for n=1
    for i in range(len(MA)):
        
        # MA[i] is an array of indices of the atoms making up the MA molecule
        MA_comz = organic[MA[i]].get_center_of_mass()[2]
        
        if(MA_comz < average_z):
            bottom_MA += sort(organic[MA[i]])
        if(MA_comz > average_z):
            upper_MA += sort(organic[MA[i]])
    if (sup == 'y'):
        upper_MA.positions[:,2] += delta_z

# The following warning can be ignored in the case of unit cell.

if (super_cell == 'super'):
    if(len(upper_MA) != len(bottom_MA)):
        print('Warning: different number of atoms in bottom and upper MA. Is this expected?')
    
    
##### OLD
##### Now we find rotation matrices between N_com and N_mol_vector vectors and apply them to
##### mol positions. We get the list "new_mol_positions" which is a list of rotated
##### mol positions. This way, we get a set of positions for the input molecule
##### (PEA.traj, BZA.traj, ...) such that the reoriented N-COM vectors are the same
##### as the ones in the template structure. Later on, we will translate these
##### "new_mol_positions" so that the positions of the N atoms will be the same as
##### in the template structure.

##### NEW
##### Here we find the rotation matrices between the desired_direction and the 
##### N_mol_vector, then we apply them to the positions of atoms that make up mol.
##### We make the list "new_mol_positions" which is a list of rotated mol atom 
##### positions. This way, we get a set of positions for the input molecule
##### (3AMP.traj,  ...) such that the reoriented N_mol_vectors are the same
##### as the desired_direction. Later on, we will spatialy translate these
##### "new_mol_positions" so that the positions of the N atoms will be the same as
##### in the template structure.


#we store the original positions for later

original = mol.get_positions()

#empty list for the rotated positions

new_mol_positions = np.empty((len(mol), 3))
new_mol_positions_flipped = np.empty((len(mol), 3))

R = get_rotation_matrix(N_mol_vector, desired_direction)
R_flipp = get_rotation_matrix(N_mol_vector, (-1)*desired_direction)

for j in range(len(mol)):
    new_mol_positions[j] = np.matmul(R, mol.get_positions()[j])
    new_mol_positions_flipped[j] = np.matmul(R_flipp, mol.get_positions()[j])

##### Writing of the final structure. For later construction of the potential,
##### it is !!!VERY!!! important that the atoms are written in the following order:
##### 1) Atoms of long molecule 1, followed by atoms of long molecule 2, etc.
##### The atoms of respective long molecules must always be in the same order
##### for every long molecule!
##### 2) Atoms of MA molecule 1, followed by atoms of MA molecule 2, etc..
##### The atoms of respective MA molecules must always be in the same order for
##### every MA molecule!
##### 3) Pb atoms
##### 4) Br atoms

'''
##### I think this is not necessary in DJP system

# We already moved MA's by delta_z, now we have to do it for the N atoms of
# the long molecules.

if (cell_type == '1'):
    # In the case of supercell, move top two N positions of the long molecules
    # first, because they have to move by 2*delta_z, once here and once a bit lower.
    top_two = np.argpartition(N_pos[:,2], -2)[-2:]
    for i in top_two:
        if (sup == 'y'):    
            N_pos[i][2] += delta_z  

if (cell_type == '2'):
    # In the case of supercell, move top four N positions of the long molecules
    # first, since they have to move by 2*delta_z.
    top_four = np.argpartition(N_pos[:,2], -4)[-4:]
    for i in top_four: 
        N_pos[i][2] += delta_z  
        
if (cell_type == '3'):
    # In the case of supercell, move the top N position of the long molecules
    # first, since they have to move 2*delta_z.
    top_one = np.argpartition(N_pos[:,2], -1)[-1:]
    for i in top_one: 
        N_pos[i][2] += delta_z  
'''

for i in range(len(N_pos)):
    # Now we move the rest of N atoms (all but the lowest ones are moved here).
    if(N_pos[i][2] > average_z):
        if (sup == 'y'):
            N_pos[i][2] += delta_z
    #### In the following lines, we write the input molecules to their new positions. ###


### for cell_type 3 ###

if (cell_type == '3'):
    for i in anchor_N_index:    
        mol.set_positions(new_mol_positions)
        # Here the translation is done so the N positions match.
        translate = N_pos[i] - mol[N_mol_indices[0]].position
        mol.positions += translate
        # The long molecule is added to the final structure.
        ordered += mol
        # The position of molecule is reset 
        mol.set_positions(original)
    
### for cell_type 2 ###

if (cell_type == '2'):
    for i in anchor_N_index:    
        if (alter == 'a' and (i%4 == 1 or i%4 == 2)):
            mol.set_positions(new_mol_positions_flipped)
            # Here the translation is done so the N positions match.
            translate = N_pos[i] - mol[N_mol_indices[0]].position
            mol.positions += translate
            # And now we compensate for rotating the molecule in the opposite direction
            flipp_correction = np.empty(3)
            flipp_correction[0] = x
            flipp_correction[1] = y
            flipp_correction[2] = z
            mol.positions += flipp_correction
            # The long molecule is added to the final structure.
            ordered += mol
            # The position of molecule is reset 
            mol.set_positions(original)
        else :
            mol.set_positions(new_mol_positions)
            # Here the translation is done so the N positions match.
            translate = N_pos[i] - mol[N_mol_indices[0]].position
            mol.positions += translate
            # The long molecule is added to the final structure.
            ordered += mol
            # The position of molecule is reset 
            mol.set_positions(original)

### for cell_type 1 ###

if (cell_type == '1'):
    for i in anchor_N_index:    
        if (alter == 'a' and i%2 == 1):
            mol.set_positions(new_mol_positions_flipped)
            # Here the translation is done so the N positions match.
            translate = N_pos[i] - mol[N_mol_indices[0]].position
            mol.positions += translate
            # And now we compensate for rotating the molecule in the opposite direction
            flipp_correction = np.empty(3)
            flipp_correction[0] = x
            flipp_correction[1] = y
            flipp_correction[2] = z
            mol.positions += flipp_correction
            # The long molecule is added to the final structure.
            ordered += mol
            # The position of molecule is reset 
            mol.set_positions(original)
        else :
            mol.set_positions(new_mol_positions)
            # Here the translation is done so the N positions match.
            translate = N_pos[i] - mol[N_mol_indices[0]].position
            mol.positions += translate
            # The long molecule is added to the final structure.
            ordered += mol
            # The position of molecule is reset 
            mol.set_positions(original)

# If n>1, MA's are written now.

if(n-1):
    
    for i in range(len(bottom_MA)):
        ordered += bottom_MA[i]
    
    for i in range(len(upper_MA)):
        ordered += upper_MA[i]

# The inorganic atoms are written. First Pb atoms and then Br atoms

ordered += inorganic_all[inorganic_all.symbols == 'Pb']
ordered += inorganic_all[inorganic_all.symbols == 'Br']

##### Finally we have to write the approximate cell.
##### Inorganic layers distances must be consistent, so that when the cell is doubled,
##### the distances between inorganic layers are the same.

bottom_inorganic_z = np.amin(ordered[(ordered.symbols == 'Pb') | (ordered.symbols == 'Br')].get_positions()[:,2])
top_inorganic_z = np.amax(ordered[(ordered.symbols == 'Pb') | (ordered.symbols == 'Br')].get_positions()[:,2])
top_organic_z = np.amax(ordered[(ordered.symbols == 'N') | (ordered.symbols == 'C') | (ordered.symbols == 'H')].get_positions()[:,2])

if (sup =='y'):
    cell_z = delta_z_factor * molecule_length * offset_projection_factor + top_inorganic_z - bottom_inorganic_z + cell_z_factor

if (sup =='n'):
    cell_z = top_organic_z - bottom_inorganic_z + cell_z_factor

# The x and y cell lengths are taken from the template.

ordered.set_cell(frame.get_cell())

# The z cell length from the template is overwritten with our cell_z.

ordered.cell[2][2] = cell_z

# We save the structure in .xyz and .traj format.
# prefix is the organic molecule we use

prefix = str(sys.argv[1]).split(".")[0]

if (cell_type == '1' or cell_type == '2'):
    if (super_cell == 'super'):
        placeholder_name = str(cell_type + 'n' + str(n) + '_' + prefix + '_' + super_cell + '_' + alter + '_' + ro + '_' + 'DJP')
    if (super_cell == 'unit'):
        placeholder_name = str(cell_type + 'n' + str(n) + '_' + prefix + '_' + super_cell + '_' + alter + '_' + 'DJP')
if (cell_type == '3'):
    if (super_cell == 'super'):
        placeholder_name = str(cell_type + 'n' + str(n) + '_' + prefix + '_' + super_cell + '_' + ro + '_' + 'DJP')
    if (super_cell == 'unit'):
        placeholder_name = str(cell_type + 'n' + str(n) + '_' + prefix + '_' + super_cell + '_' + 'DJP')


print('Enter prefix of the name of output file [' + placeholder_name + ']')
name = input()

if(len(name) == 0):
    name = placeholder_name

write(name + '.traj', ordered)
write(name + '.xyz', ordered)
