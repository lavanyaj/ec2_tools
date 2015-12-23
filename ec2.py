"""
ec2.py
~~~~~~

Simple EC2 cluster management with Python, designed to make it easy to
name and work with clusters, and to integrate with `fabric`.  

For usage information see README.md.
"""

#### Library imports

# Standard library
import os
import shelve
import subprocess
import sys
import time
import argparse

# Third party libraries
import boto.ec2
import boto
#boto.set_stream_logger('boto')
# My libraries
import ec2_classes

#### Constants and globals

# The list of EC2 AMIs to use, from alestic.com
# AMIS = {"m1.small" : "ami-e2af508b",
#         "c1.medium" : "ami-e2af508b",
#         "m1.large" : "ami-68ad5201",
#         "m1.xlarge" : "ami-68ad5201",
#         "m2.xlarge" : "ami-68ad5201",
#         "m2.2xlarge" : "ami-68ad5201",
#         "m2.4xlarge" : "ami-68ad5201",
#         "c1.xlarge" : "ami-68ad5201",
#         "cc1.4xlarge" : "ami-1cad5275"
#         }

# The most important data structure we use is a persistent shelf which
# is used to represent all the clusters.  The keys in this shelf are
# the `cluster_names`, and the values will be ec2_classes.Cluster
# objects, which represent named EC2 clusters.
#
# The shelf will be stored at "HOME/.ec2-shelf"
HOME = os.environ["HOME"]



# Check that the required environment variables exist
def check_environment_variables_exist(*args):
    """
    Check that the environment variables in `*args` have all been
    defined.  If any do not, print an error message and exit.
    """
    vars_exist = True
    for var in args:
        if var not in os.environ:
            print "Need to set $%s environment variable" % var
            vars_exist = False
    if not vars_exist:
        print "Exiting"
        sys.exit()

check_environment_variables_exist(
    "AWS_HOME", "AWS_KEYPAIR", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")

# EC2 connection object
#ec2_conn = EC2Connection(
#    os.environ["AWS_ACCESS_KEY_ID"], os.environ["AWS_SECRET_ACCESS_KEY"])

# make sure to configure boto as decribed here http://boto.cloudhackers.com/en/latest/getting_started.html
ec2_conn = boto.ec2.connect_to_region("us-west-2")

#### The following are the functions corresponding to the command line
#### API calls: create, show, show_all etc.

def create(cluster_name, n, instance_type, ami):
    """
    Create an EC2 cluster with name `cluster_name`, and `n` instances
    of type `instance_type`.  Update the `clusters` shelf to include a
    description of the new cluster.
    """
    # Parameter check
    if exists(cluster_name):
        print ("A cluster with name %s already exists.  Exiting." 
               % cluster_name)
        sys.exit()
    if n < 1 or n > 20:
        print "Clusters must contain between 1 and 20 instances.  Exiting."
        sys.exit()
    clusters = shelve.open("%s/.ec2-shelf" % HOME, writeback=True)
    #     if not instance_type in AMIS:
    #         print "Instance type not recognized, setting it to be 'm1.small'."
    #         instance_type = "m1.small"
    # Create the EC2 instances
    instances = create_ec2_instances(n, instance_type, ami)
    # Update clusters
    clusters[cluster_name] = ec2_classes.Cluster(
        cluster_name, instance_type, ami, instances)
    clusters.close()

def show(cluster_name):
    """
    Print the details of cluster `cluster_name` to stdout.
    """
    cluster = get_cluster(cluster_name)
    print "Displaying instances from cluster: %s" % cluster_name
    print "Instances of type: %s" % cluster.instance_type
    print "{0:8}{1:13}{2:35}".format(
        "index", "EC2 id", "public dns name")
    for (j, instance) in enumerate(cluster.instances):
        print "{0:8}{1:13}{2:35}".format(
            str(j), instance.id, instance.public_dns_name)

def show_all():
    """
    Print the details of all clusters to stdout.
    """
    clusters = shelve.open("%s/.ec2-shelf" % HOME, writeback=True)
    if len(clusters) == 0:
        print "No clusters present."
        clusters.close()
        sys.exit()
    print "Showing all clusters."
    for cluster_name in clusters:
        show(cluster_name)
    clusters.close()

def shutdown(cluster_name):
    """
    Shutdown all EC2 instances in ``cluster_name``, and remove
    ``cluster_name`` from the shelf of clusters.
    """
    if not exists(cluster_name):
        print "No cluster with that name."
        sys.exit()
    print "Shutting down cluster %s." % cluster_name
    clusters = shelve.open("%s/.ec2-shelf" % HOME, writeback=True)
    ec2_conn.terminate_instances(
        [instance.id for instance in clusters[cluster_name].instances])
    del clusters[cluster_name]
    clusters.sync()
    clusters.close()

def shutdown_all():
    """
    Shutdown all EC2 instances in all clusters, and remove all
    clusters from the `clusters` shelf.
    """
    clusters = shelve.open("%s/.ec2-shelf" % HOME, writeback=True)
    if len(clusters) == 0:
        print "No clusters to shut down.  Exiting."
        clusters.close()
        sys.exit()
    for cluster_name in clusters:
        shutdown(cluster_name)
    clusters.close()

def login(cluster_name, instance_index):
    """
    ssh to `instance_index` in `cluster_name`.
    """
    cluster = get_cluster(cluster_name)
    instance = get_instance(cluster, instance_index)
    print "SSHing to instance with address %s" % (instance.public_dns_name)
    keypair = "%s/%s.pem" % (os.environ["AWS_HOME"], os.environ["AWS_KEYPAIR"])
    os.system("ssh -i %s -o StrictHostKeyChecking=no ubuntu@%s" % (keypair, instance.public_dns_name))

def kill(cluster_name, instance_index):
    """
    Shutdown instance `instance_index` in cluster `cluster_name`, and
    remove from the clusters shelf.  If we're killing off the last
    instance in the cluster then it runs `shutdown(cluster_name)`
    instead.
    """
    cluster = get_cluster(cluster_name)
    instance = get_instance(cluster, instance_index)
    if size(cluster_name)==1:
        print "Last machine in cluster, shutting down entire cluster."
        shutdown(cluster_name)
        sys.exit()
    print ("Shutting down instance %s on cluster %s." % 
           (instance_index, cluster_name))
    ec2_conn.terminate_instances([instance.id])
    del cluster.instances[instance_index]
    clusters = shelve.open("%s/.ec2-shelf" % HOME, writeback=True)
    clusters[cluster_name] = cluster
    clusters.close()

def add(cluster_name, n):
    """
    Add `n` instances to `cluster_name`, of the same instance type as
    the other instances already in the cluster.
    """
    cluster = get_cluster(cluster_name)
    if n < 1:
        print "Must be adding at least 1 instance to the cluster.  Exiting."
        sys.exit()
    # Create the EC2 instances
    instances = create_ec2_instances(n, cluster.instance_type, cluster.ami)
    # Update clusters
    cluster.add(instances)
    clusters = shelve.open("%s/.ec2-shelf" % HOME, writeback=True)
    clusters[cluster_name] = cluster
    clusters.close()

def ssh(cluster_name, instance_index, cmd, background=False):
    """
    Run `cmd` on instance number `instance_index` on `cluster_name`.

    Runs in the background if `background == True`.  This feature is
    not currently exposed from the command line API, but may be useful
    in future.
    """
    cluster = get_cluster(cluster_name)
    instance = get_instance(cluster, instance_index)
    keypair = "%s/%s.pem" % (os.environ["AWS_HOME"], os.environ["AWS_KEYPAIR"])
    append = {True: " &", False: ""}[background]
    remote_cmd = ("'nohup %s > foo.out 2> foo.err < /dev/null %s'" %
                  (cmd, append))
    os.system(("ssh -o StrictHostKeyChecking=no -o BatchMode=yes -i %s ubuntu@%s %s" %
               (keypair, instance.public_dns_name, remote_cmd)))

def ssh_all(cluster_name, cmd):
    """
    Run `cmd` on all instances in `cluster_name`.
    """
    cluster = get_cluster(cluster_name)
    for j in range(size(cluster_name)):
        ssh(cluster_name, j, cmd)

def scp(cluster_name, instance_index, local_filename, remote_filename=False):
    """
    scp `local_filename` to `remote_filename` on instance
    `instance_index` on cluster `cluster_name`.  If `remote_filename`
    is not set or is set to `False` then `remote_filename` is set to
    `local_filename`.
    """
    cluster = get_cluster(cluster_name)
    instance = get_instance(cluster, instance_index)
    keypair = "%s/%s.pem" % (os.environ["AWS_HOME"], os.environ["AWS_KEYPAIR"])
    if not remote_filename:
        remote_filename = "."
    os.system(("scp -r -i %s %s ubuntu@%s:%s" %
               (keypair, local_filename, 
               instance.public_dns_name, remote_filename)))

def scp_all(cluster_name, local_filename, remote_filename=False):
    """
    Run `scp` on all instances in `cluster_name`.
    """
    for j in range(size(cluster_name)):
        scp(cluster_name, j, local_filename, remote_filename)

#### Helper functions

def create_ec2_instances(n, instance_type, ami):
    """
    Create an EC2 cluster with `n` instances of type `instance_type`.
    Return the corresponding boto `reservation.instances` object.
    This code is used by both the `create` and `add` functions, which
    is why it was factored out.
    """
    #ami = AMIS[instance_type]
    image = ec2_conn.get_all_images(image_ids=[ami])[0]
    reservation = image.run(
        n, n, os.environ["AWS_KEYPAIR"], instance_type=instance_type)
    for instance in reservation.instances:  # Wait for the cluster to come up
        while instance.update()== u'pending':
            time.sleep(1)
    return reservation.instances

def get_cluster(cluster_name):
    """
    Check that a cluster with name `cluster_name` exists, and return
    the corresponding Cluster object if so.
    """
    clusters = shelve.open("%s/.ec2-shelf" % HOME, writeback=True)
    if cluster_name not in clusters:
        print "No cluster with the name %s exists.  Exiting." % cluster_name
        clusters.close()
        sys.exit()
    cluster = clusters[cluster_name]
    clusters.close()
    return cluster

def get_instance(cluster, instance_index):
    """
    Check that ``cluster`` has an instance with index
    ``instance_index``, and if so return the corresponding
    ``ec2_classes.Instance object``.
    """
    try:
        return cluster.instances[instance_index]
    except IndexError:
        print ("The instance index must be in the range 0 to %s. Exiting." %
               (len(cluster.instances)-1,))
        sys.exit()

#### Methods to export externally

def exists(cluster_name):
    """
    Return ``True`` if an EC2 cluster with name ``cluster_name`` exists, and
    ``False`` otherwise.
    """
    clusters = shelve.open("%s/.ec2-shelf" % HOME, writeback=True)
    value = cluster_name in clusters
    clusters.close()
    return value

def public_dns_names(cluster_name):
    """
    Return a list containing the public dns names for `cluster_name`.

    See README.md to see how this enables easy integration with
    Fabric.
    """
    clusters = shelve.open("%s/.ec2-shelf" % HOME, writeback=True)
    if cluster_name not in clusters:
        print (
            "Cluster name %s not recognized.  Exiting ec2.public_dns_names()." %
               cluster_name)
        clusters.close()
        sys.exit()
    else:
        cluster = clusters[cluster_name]
        clusters.close()
        return ["ubuntu@%s"%(instance.public_dns_name) for instance in cluster.instances]

def size(cluster_name):
    """
    Return the size of the cluster with name ``cluster_name``.
    """
    return len(get_cluster(cluster_name).instances)

#### External interface


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Manage EC2 instances.')

    parser.add_argument('--create', action='store_true')
    parser.add_argument('--cluster_name', default='CLUSTER_NAME')
    parser.add_argument('--num_instances', default=1)
    parser.add_argument('--type', default='m3.xlarge')
    parser.add_argument('--ami', default='ami-02938c63')

    parser.add_argument('--show', action='store_true')
    parser.add_argument('--show_all', action='store_true')
    parser.add_argument('--shutdown', action='store_true')
    parser.add_argument('--shutdown_all', action='store_true')
    parser.add_argument('--add', action='store_true')

    args = parser.parse_args()

    
    if args.create:
        create(args.cluster_name, int(args.num_instances), args.type, args.ami)
    elif args.show:
        show(args.cluster_name)
    elif args.show_all:
        show_all()
    elif args.shutdown:
        shutdown(args.cluster_name)
    elif args.shutdown_all:
        shutdown_all()
    elif args.add:
        add(args.cluster_name, int(args.num_instances))
        pass
    else:
        print ("Command not recognized. ")
        pass

    """
    elif cmd=="login" and l==2:
        login(args[1], 0)
    elif cmd=="login" and l==3:
        login(args[1], int(args[2]))
    elif cmd=="kill" and l==3:
        kill(args[1], int(args[2]))
    elif cmd=="ssh" and l==4:
        ssh(args[1], int(args[2]), args[3])
    elif cmd=="ssh_all" and l==3:
        ssh_all(args[1], args[2])
    elif cmd=="scp" and l==4:
        scp(args[1], int(args[2]), args[3])
    elif cmd=="scp" and l==5:
        scp(args[1], int(args[2]), args[3], args[4])
    elif cmd=="scp_all" and l==3:
        scp_all(args[1], args[2])
    elif cmd=="scp_all" and l==4:
        scp_all(args[1], args[2], args[3])
    """
